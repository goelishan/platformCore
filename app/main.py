from contextlib import asynccontextmanager
from fastapi import FastAPI
import os
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    # Smoke-test DB connectivity before accepting traffic. If this raises,
    # the app never starts, healthcheck never passes, nginx stays unrouted.
    # That's the correct failure mode — fail fast on broken dependencies.
    #
    # Sync psycopg is fine here because we're blocking the event loop at
    # startup (no requests in flight). In request handlers, use async psycopg.
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            print("DB says:", cur.fetchone()[0], flush=True)

    yield  # --- App runs here ---

    # --- Shutdown ---
    # Nothing to clean up today. This is where we'd close connection pools,
    # cancel background tasks, flush metrics, or signal a drain state.
    # Runs on SIGTERM; must complete within K8s terminationGracePeriodSeconds
    # (default 30s) or the container gets SIGKILLed mid-cleanup.
    print("lifespan: shutdown complete", flush=True)


app = FastAPI(lifespan=lifespan)


@app.get("/")
def root():
    return {"service": "platformcore", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}