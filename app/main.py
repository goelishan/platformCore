# PlatformCore FastAPI app 
# - Auth: Pod IRSA annotation → webhook injects AWS creds → boto3 calls
#   sts:AssumeRoleWithWebIdentity → generate_db_auth_token signs a 15-min
#   token passed as the DB password. No static secret, no rotation needed.
# - Token lifecycle: expires every 15 min, regenerated per connection open.
#   Safe here because connections are per-request; pooling would require
#   explicit token-refresh logic before reuse.
# - Health split: /health (liveness) is DB-free — never restart on DB outage.
#   /ready (readiness) touches DB — pulls Pod from rotation without restarting,
#   and caches its last success briefly to bound probe-driven connection churn.


from fastapi import FastAPI, HTTPException
import os
import time
import boto3
from prometheus_fastapi_instrumentator import Instrumentator

import logging
import json

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        })
    
logger = logging.getLogger("platformcore")

handler=logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logging.root.setLevel(logging.INFO)
logging.root.handlers = [handler]

app = FastAPI(title="platformcore")
Instrumentator().instrument(app).expose(app)


def get_rds_auth_token():
    """Generate a short-lived IAM auth token for RDS."""

    client=boto3.client("rds",region_name=os.environ["AWS_DEFAULT_REGION"])
    return client.generate_db_auth_token(
        DBHostname=os.environ["RDS_HOST"],
        Port=int(os.environ.get("RDS_PORT", "5432")),
        DBUsername=os.environ["RDS_USER"],
        Region=os.environ["AWS_DEFAULT_REGION"],
    )


def get_db_connection():
    """Open a psycopg connection using an IAM auth token as the password.
    """
    for var in ("RDS_HOST", "RDS_USER", "RDS_DB_NAME", "AWS_DEFAULT_REGION"):
        if not os.environ.get(var):
            logger.error(f"missing required env var: {var}")
            raise HTTPException(
                status_code=503,
                detail=f"{var} not configured; DB routes unavailable",
            )
    import psycopg
    token = get_rds_auth_token()
    conn_str = (
        f"host={os.environ['RDS_HOST']} "
        f"port={os.environ.get('RDS_PORT', '5432')} "
        f"dbname={os.environ['RDS_DB_NAME']} "
        f"user={os.environ['RDS_USER']} "
        f"password={token} "
        f"sslmode=require"
    )
    return psycopg.connect(conn_str)


@app.get("/")
def root():
    return {"service": "platformcore", "status": "ok"}


@app.get("/health")
def health():
    """Liveness probe. DB-free by design."""
    return {"status": "healthy"}


# Wired to a readiness probe, /ready runs on every period, on every replica, forever,
# and every call opens a fresh psycopg connection — a TLS handshake and an auth round
# trip charged to RDS to re-answer a question answered seconds ago. A successful check
# is therefore cached for a TTL that ships beside the probe period in the chart, which
# halves the connection rate at the chart's defaults. Failures are never cached, so a
# database that recovers is seen on the very next probe. The cost is bounded and
# deliberate: a cached success can hide an outage for at most one TTL, putting worst
# case detection at TTL + periodSeconds x failureThreshold, or about 20 seconds.
READY_CACHE_TTL_SECONDS = float(os.environ.get("READY_CACHE_TTL_SECONDS", "10"))

_ready_ok_until = 0.0  # monotonic deadline of the cached success; 0.0 means none held


def _reset_ready_cache():
    """Drop the cached success. Nothing on the request path calls this; tests do,
    because the cache is process-global and would otherwise leak between them."""
    global _ready_ok_until
    _ready_ok_until = 0.0


@app.get("/ready")
def ready():
    """Readiness probe. Confirms RDS reachability via IAM auth token."""
    global _ready_ok_until
    now = time.monotonic()
    if now < _ready_ok_until:
        return {"status": "ready"}
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"readiness check failed: {e}")
        raise HTTPException(status_code=503, detail=f"DB unreachable: {e}")
    _ready_ok_until = now + READY_CACHE_TTL_SECONDS
    return {"status": "ready"}


@app.get("/version")
def version():
    return {"image_tag": os.environ.get("APP_VERSION", "unknown")}