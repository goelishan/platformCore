# PlatformCore FastAPI app — Phase 9 lazy-DB variant.
#
# Why lazy: we are proving ALB + container + VPC-endpoints end-to-end BEFORE
# deploying a database (RDS lands in Phase 10). If the app required a DB at
# startup, the container would crash-loop, the target group would never flip
# healthy, and we could not prove the infra path in isolation.
#
# Design: split health signals into two kinds. This is a production-grade
# distinction worth defending in interviews.
#
#   /health -> LIVENESS: "is the process serving HTTP?"
#              MUST NOT touch external dependencies. If a DB outage flipped
#              this endpoint to failing, the container would restart-loop
#              while the DB recovers, making things strictly worse. Keep it
#              trivial. This is what tells the orchestrator whether to kill
#              and restart the container.
#
#   /ready  -> READINESS: "can I serve traffic that needs downstream deps?"
#              Touches the DB. When this fails, the load balancer removes us
#              from rotation but the container keeps running. Traffic
#              returns automatically once the DB recovers.
#
# The ALB target group health check currently points at /health (liveness),
# which is why the app can pass health checks with zero DB configuration.
# When RDS lands in Phase 10, we will re-point the ALB health check to
# /ready to get proper readiness semantics — at that point a DB outage will
# briefly take the instance out of rotation (correct behavior) instead of
# restart-looping it (incorrect behavior).

from fastapi import FastAPI, HTTPException
import os

app = FastAPI(title="platformcore")


def get_db_connection():
    """Lazy DB connection. Only called from routes that actually need the DB.

    psycopg is imported locally so the module itself stays importable when
    the driver or env var is missing — useful for bootstrap (Phase 9) and
    for CI smoke tests that never touch a real database.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # 503 Service Unavailable: the dependency is not configured.
        # The app is alive; only DB-backed routes are degraded.
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL not configured; DB routes unavailable",
        )
    import psycopg

    return psycopg.connect(db_url)


@app.get("/")
def root():
    """Default route — DB-free. Useful for smoke testing via curl through
    the ALB. Returns a fixed JSON payload so an interviewer watching `curl`
    output gets an immediate 'the service is up' signal."""
    return {"service": "platformcore", "status": "ok"}


@app.get("/health")
def health():
    """Liveness probe. DB-free by design; see module docstring. Wired to the
    ALB target group health_check.path today."""
    return {"status": "healthy"}


@app.get("/ready")
def ready():
    """Readiness probe. Confirms DB reachability via a trivial SELECT 1.

    503 when DB is unreachable or unconfigured (which is the expected state
    in Phase 9 — no RDS exists yet). The ALB health check will move here
    when Phase 10 deploys RDS.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except HTTPException:
        # Re-raise to preserve the 503 from get_db_connection.
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB unreachable: {e}")
    return {"status": "ready"}


@app.get("/version")
def version():
    """Returns the running image tag if set via the APP_VERSION env var.

    Useful for verifying which image is currently live when iterating via
    ECR pushes. The deploy pipeline (user_data in ec2.tf today; GitHub
    Actions in Phase 10) is responsible for injecting APP_VERSION to match
    the image tag it pulled.
    """
    return {"image_tag": os.environ.get("APP_VERSION", "unknown")}
