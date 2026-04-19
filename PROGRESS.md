# PlatformCore — Progress Log

PlatformCore is the hands-on thread for Ishan's 40-day DBA → Platform Engineer transition. The full roadmap lives at [`docs/platform_engineer_40day_roadmap.html`](docs/platform_engineer_40day_roadmap.html).

**GitHub remote:** https://github.com/goelishan/platformCore

**This file is a safety net.** It is updated at the end of every work session so that progress is never lost — even if external tools or memory systems fail. `git log` is the second safety net. Memory files are the third.

---

## Current position

- **Phase:** 1 — Docker Compose (Days 1–5, Gate 1)
- **Day:** Day 3 completed on 2026-04-19
- **Gate 1 deliverable:** PlatformCore local stack — app + nginx + postgres, fully wired, health-gated, no hardcoded secrets. `docker compose up` works cold.

---

## Phase 1 — Docker Compose

### Done

**Day 1 — Basic stack up**
- Initial `docker-compose.yml` with `app` (FastAPI, port 8000) and `postgres:16`.
- `app/Dockerfile` (python:3.12-slim, uvicorn entrypoint) and `requirements.txt` (fastapi, uvicorn[standard], psycopg).
- `app/main.py` — FastAPI with `/` and `/health` endpoints.
- `DATABASE_URL` wired from compose env into the app.

**Day 2 — Health-gated startup**
- Added Postgres `healthcheck` using `pg_isready -U platformcore -d platformcore`.
- Added `depends_on: condition: service_healthy` on the app so it only starts after Postgres is accepting connections.
- Added named volume `pgdata` mounted at `/var/lib/postgresql/data` so DB state survives container restarts.
- Added a FastAPI startup hook in `main.py` that runs `SELECT version()` against Postgres and logs the result — proves end-to-end connectivity on every boot.

**Day 3 — Nginx, secrets externalised, network segmentation**
- Added `nginx:1.27-alpine` service as the reverse proxy, listening on host `:80`. `nginx/default.conf` defines an `app` upstream and forwards standard proxy headers (`Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`).
- Removed the app's `ports: 8000:8000` binding — the app is no longer directly reachable from the host; traffic must enter through nginx.
- Added two named networks: `edge` (nginx ↔ app) and `backend` (app ↔ postgres). Nginx has no route to Postgres at all — defence-in-depth at the Docker layer.
- Moved `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` into a root `.env` file. Compose does variable substitution into `docker-compose.yml`, so the YAML holds zero secret literals. Committed `.env.example` as a template; `.gitignore` excludes `.env` itself.
- `DATABASE_URL` is now assembled from the substituted variables, single source of truth.
- Added healthchecks on all three services. Startup order: postgres healthy → app healthy → nginx healthy.
- **Bug fix during verification:** nginx initially reported `unhealthy`. Root cause: `listen 80;` was IPv4-only, but alpine's musl resolver returns `::1` for `localhost`, so the in-container `wget` probe hit a closed IPv6 port. Fixed by (a) adding `listen [::]:80;` so nginx binds both families, and (b) switching the nginx healthcheck to a dedicated `/_nginx_health` local endpoint that doesn't proxy through to the app. This second change decouples nginx's health from the app's — interview-correct pattern (each container's healthcheck tests its own primary process, not its dependencies).
- **Gate 1 verified end-to-end on 2026-04-19:** `docker compose up --build` from cold → all three services healthy, external `curl http://localhost/` hits app via nginx, `curl http://localhost:8000/` refused, network segmentation confirmed by `docker inspect` (nginx on edge only, app on both, postgres on backend only).

### Outstanding (Days 4–5)

- [ ] Restart policies on services
- [ ] Bind-mount source into the app container for live dev
- [ ] `docker-compose.override.yml` — dev vs prod split (re-expose app port for dev debugging here, not in base)
- [ ] Compose + CI/CD integration
- [ ] Migrate FastAPI `@app.on_event("startup")` to the modern `lifespan` context manager (interview-visible cleanup flagged in day-3 rapid fire)
- [ ] Meaningful git commits per day

---

## Session log

### 2026-04-19 — Session
- Confirmed the 40-day roadmap and committed it to persistent memory.
- Set up safety nets: `PROGRESS.md` (this file), roadmap HTML copied to `docs/`, memory files written for user profile, roadmap, project state, reference, session ritual, and no-direct-code-writes rule.
- Established session ritual: rapid-fire recall round on prior days before starting any new day.
- Ran rapid fire on days 1–2: 5.5/8 substantively correct. Gaps: Docker embedded DNS (service-name resolution via 127.0.0.11), `EXPOSE` being metadata only, and FastAPI `lifespan` replacing `@app.on_event`. All three addressed in the grading.
- Day 3 shipped: nginx reverse proxy, secrets externalised via `.env`, network segmentation (edge + backend), healthchecks on all three services.
- Caught and fixed an IPv6/IPv4 listener bug in the nginx healthcheck during live verification; added `/_nginx_health` local endpoint to decouple nginx's health from the app.
- Gate 1 verified end-to-end. All three containers healthy.
- Open action: Ishan to run git commits manually (sandbox can't delete stale `.git/index.lock`). One commit for day 1–2 baseline, one for day 3.
