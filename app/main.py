# PlatformCore FastAPI app 
# - Auth: Pod IRSA annotation → webhook injects AWS creds → boto3 calls
#   sts:AssumeRoleWithWebIdentity → generate_db_auth_token signs a 15-min
#   token passed as the DB password. No static secret, no rotation needed.
# - Token lifecycle: expires every 15 min, regenerated per connection open.
#   Safe here because connections are per-request; pooling would require
#   explicit token-refresh logic before reuse.
# - Health split: /health (liveness) is DB-free — never restart on DB outage.
#   /ready (readiness) touches DB — pulls Pod from rotation without restarting.


from fastapi import FastAPI, HTTPException
import os
import boto3

app = FastAPI(title="platformcore")

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

@app.get("/ready")
def ready():
    """Readiness probe. Confirms RDS reachability via IAM auth token."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB unreachable: {e}")
    return {"status": "ready"}


@app.get("/version")
def version():
    return {"image_tag": os.environ.get("APP_VERSION", "unknown")}