from fastapi import FastAPI
import os
import psycopg

app = FastAPI()
DATABASE_URL = os.environ["DATABASE_URL"]

@app.on_event("startup")
def check_db():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            print("DB says:", cur.fetchone()[0], flush=True)

@app.get("/")
def root():
    return {"service": "platformcore", "status": "ok"}

@app.get("/health")
def health():
    return {"status": "healthy"}