# PlatformCore — Progress Log

PlatformCore is the hands-on thread for Ishan's 40-day DBA → Platform Engineer transition. The full roadmap lives at [`docs/platform_engineer_40day_roadmap.html`](docs/platform_engineer_40day_roadmap.html).

**GitHub remote:** https://github.com/goelishan/platformCore

**This file is a safety net.** It is updated at the end of every work session so that progress is never lost — even if external tools or memory systems fail. `git log` is the second safety net. Memory files are the third.

---

## Current position

- **Phase:** 1 — Docker Compose (Days 1–5, Gate 1)
- **Day:** Day 4 completed and verified on 2026-04-20. Day 5 begins next session.
- **Gate 1 deliverable:** PlatformCore local stack — app + nginx + postgres, fully wired, health-gated, no hardcoded secrets. `docker compose up` works cold. **Met as of end of day 3; hardened on day 4 with restart policies, dev/prod split, and modern FastAPI lifespan.**

## Open actions (for user to complete before/at next session)

- [x] First commit pushed to `https://github.com/goelishan/platformCore` on 2026-04-19 — confirmed successful by user.
- [ ] Commit and push day 4 work (commit message draft at the end of this file).

## Day 5 agenda (tentative — confirm at session start)

- Rapid-fire round on day 4 material (restart policy semantics, `docker kill` bypassing `unless-stopped` via the `manuallyStopped` flag, `unless-stopped` vs `on-failure` semantics, override auto-merge rules, lifespan vs `on_event`, graceful shutdown / exit codes, `terminationGracePeriodSeconds`).
- Day 5 is the Phase 1 closer. Pick from at session start:
  - Compose + CI/CD integration (first pass — a GitHub Actions workflow that at minimum runs `docker compose config` as a PR gate).
  - Introduce a `docker-compose.prod.yml` as an explicit third file so the base + dev-override + prod-overlay pattern becomes tangible before K8s.
  - Add a one-shot migration/seed-data container (applies `on-failure` restart policy correctly — natural contrast to `unless-stopped` for long-running services).
  - Phase 1 closeout: write an interview-ready narrative + topology diagram for what's been built.

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

**Day 4 — Restart policies, dev/prod split, modern lifespan**

- Added `restart: unless-stopped` to nginx, app, and postgres. Level-1 self-healing at the Docker layer: organic process exits (OOM, segfault, internal crash of PID 1) trigger a restart-in-place — same container ID, fresh uptime, healthcheck re-green within seconds. `docker stop`/`docker kill` set a `manuallyStopped` flag that intentionally bypasses the policy, so real crashes were simulated with `docker compose exec postgres bash -c 'kill -9 1'` to trigger an organic exit. Also confirmed `docker compose stop app` correctly leaves the container dead (respecting human intent), verifying the `unless` half of `unless-stopped`.
- Split the compose topology into a prod-shaped base (`docker-compose.yml`) and a dev-only overlay (`docker-compose.override.yml`). Base has zero dev-only concerns: no bind-mount, no `--reload`, no host app port. The override adds the live-dev bind-mount `./app → /app`, the `uvicorn ... --reload` command, and `ports: 8000:8000` for direct-to-app debugging. Compose auto-merges them on `docker compose up`; `docker compose -f docker-compose.yml up` skips the override to simulate prod locally. Verified both modes empirically — `main.py` edits reflect instantly in dev mode and have no effect in prod-shape mode.
- Added `watchfiles` to `app/requirements.txt` so uvicorn's `--reload` uses the fast inotify-backed watcher. Proof of correctness is the `using WatchFiles` line in the uvicorn startup logs (fallback would show `using StatReload`).
- Migrated `app/main.py` from deprecated `@app.on_event("startup")` to a `lifespan` async context manager. Pre-`yield` holds the DB connectivity smoke test (`SELECT version()`); post-`yield` is the shutdown hook (placeholder print today; future phases will close pools, flush metrics, cancel background tasks here). Verified the full lifespan contract via `docker compose stop app`: `lifespan: shutdown complete` logged between `Waiting for application shutdown` and `Application shutdown complete`, and `app-1 exited with code 0` — the canonical graceful-shutdown signal (non-signal exit code with full shutdown log lines preceding the exit). `exit 143` would indicate SIGTERM not handled; `exit 137` would indicate SIGKILL timeout. Neither appeared.

**Day 4 design story (interview-ready):**

Day 4 converted PlatformCore from a file that blended dev and prod concerns into a proper two-mode stack. `restart: unless-stopped` gives us level-1 self-healing — docker auto-relaunches dead containers on organic exits while respecting explicit human stops. `docker-compose.override.yml` separates dev-only concerns (bind-mount, `--reload`, direct app port) from a production-shaped base, mirroring the base+overlay pattern that reappears as Kustomize overlays in K8s and values files in Helm — same architecture, different tools. The deprecated FastAPI `@app.on_event("startup")` hook was replaced with a `lifespan` context manager, giving us a structurally-correct shutdown path (one function, shared scope, shutdown slot impossible to forget) and verified graceful SIGTERM (exit 0, post-`yield` print fired before exit). What would break it: a bind-mount leaking into a prod deployment via an orphaned override file (mitigated by explicit `-f` flags in prod and by not shipping override files to prod artifacts), or shutdown code that exceeds Kubernetes' `terminationGracePeriodSeconds` (default 30s) and gets SIGKILLed mid-cleanup before connection pools close cleanly.

### Outstanding (Day 5)

- [ ] Compose + CI/CD integration (first pass — `docker compose config` as a PR gate in GitHub Actions).
- [ ] Meaningful git commits per day (day 4 commit still pending when this was written).

---

## Session log

### 2026-04-19 — Session
- Confirmed the 40-day roadmap and committed it to persistent memory.
- Set up safety nets: `PROGRESS.md` (this file), roadmap HTML copied to `docs/`, memory files for user profile, roadmap, project state, reference, session ritual, and no-direct-code-writes rule.
- Established session ritual: rapid-fire recall round on prior days before starting any new day.
- Ran rapid fire on days 1–2: 5.5/8 substantively correct. Gaps: Docker embedded DNS (service-name resolution via 127.0.0.11), `EXPOSE` being metadata only, and FastAPI `lifespan` replacing `@app.on_event`. All three addressed in the grading.
- Day 3 shipped: nginx reverse proxy, secrets externalised via `.env`, network segmentation (edge + backend), healthchecks on all three services.
- Caught and fixed an IPv6/IPv4 listener bug in the nginx healthcheck during live verification; added `/_nginx_health` local endpoint to decouple nginx's health from the app.
- Gate 1 verified end-to-end. All three containers healthy.
- Drafted comprehensive `.gitignore` (anticipates Terraform + K8s) and `README.md` for a portfolio-quality initial commit.
- Remote set to `https://github.com/goelishan/platformCore`. Initial commit pushed successfully (user confirmed, no errors).
- Session closed. Day 3 locked and on GitHub; day 4 agenda queued in "Current position" above.

### 2026-04-20 — Session

- Ran rapid-fire round on day 3 material: 3 clean (network segmentation + K8s NetworkPolicy connection, musl/IPv6 mechanism, `compose down` vs `down -v`), 3 partial-but-structurally-correct (healthcheck cascade intent right but concrete cascade missing, scaling ordering structurally sound but wrong first-break, failure-mode narrative right but missed current-state nuance), 2 confused (`.env` vs `env_file:` mechanism flipped, `docker inspect` port output wrong). User initially framed this as "disgraceful"; reframed with data — structural intuition ahead of mechanism detail is the *correct* learning direction for Platform Engineer interviews, and calibrated uncertainty ("I was guessing") is a strength in interview settings, not a weakness.
- Before day 4 work, ran two empirical gap-fixes: (a) `.env` vs `env_file:` — added `TEST_VAR=from_root_env` to `.env`, confirmed it's invisible to the container until `env_file: - .env` is added; confirmed the root `.env` shared across services causes credential leakage (`POSTGRES_*` vars ended up in the app container). This converted a conceptual confusion into a lived experiment and banked the "per-service env files, principle of least privilege" interview answer. Rolled back the override after verification.
- Day 4 shipped cleanly:
  - `restart: unless-stopped` on all three services; tested via `docker kill` (did not restart, by design — `manuallyStopped` flag), then via in-container `kill -9 1` (restarted correctly, same container ID, fresh uptime), then via `docker compose stop` (correctly stayed dead, respecting human intent).
  - Bind-mount + `uvicorn --reload` + `watchfiles` stood up; live-edit loop verified via `curl /health` reflecting edited payloads within ~1s.
  - `docker-compose.override.yml` introduced; both `docker compose up` (dev, override merged) and `docker compose -f docker-compose.yml up` (prod-shape, override skipped) verified — dev mode reflects live edits, prod-shape mode does not, direct `curl :8000` works only in dev.
  - `app/main.py` migrated from `@app.on_event("startup")` → `lifespan` async context manager. Shutdown path verified via `docker compose stop app`: `lifespan: shutdown complete` logged correctly between shutdown-waiting and shutdown-complete lines; `app-1 exited with code 0`.
- Interview hooks banked this session: shared `.env` = credential leakage across services; `docker kill` vs in-container `kill -9 1` (restart policy semantics depend on where the signal originates); `unless-stopped` vs `on-failure` (long-running services vs finite jobs); `docker-compose.override.yml` maps directly to Kustomize overlays + Helm values files; lifespan graceful shutdown and its K8s `terminationGracePeriodSeconds` tie-in; exit code 0 vs 143 vs 137 as graceful-shutdown signals.
- Cross-domain moment noted: user's DBA background surfaced the postgres buffer cache concern when discussing restart policies. Filed the full mental model — `shared_buffers` lost on container exit, OS page cache may survive container restart but not host reboot, interview-worthy tie to why stateful workloads are "pets not cattle" and the role of PodDisruptionBudgets, StatefulSets, read replicas, `pg_prewarm`, PgBouncer, and postgres operators.
- Day 4 closed. Commit pending (user to author and push).

---

## Day 4 commit message draft

Below is a starting point — feel free to tighten further:

```
Phase 1 Day 4: restart policies, dev/prod split, FastAPI lifespan

- Add `restart: unless-stopped` to all three services for level-1
  self-healing. Docker auto-restarts on organic exits (OOM, crash)
  and respects human-initiated stops (`docker stop`/`kill` set a
  manuallyStopped flag that bypasses the policy — verified by
  simulating a real crash via in-container `kill -9 1`).

- Split compose into prod-shaped base + dev-only overlay.
  docker-compose.override.yml adds the bind-mount (./app → /app),
  `uvicorn --reload`, and direct app port (8000:8000) only in dev.
  `docker compose up` auto-merges; `docker compose -f
  docker-compose.yml up` simulates prod locally.

- Add watchfiles to requirements.txt so uvicorn's --reload uses
  the inotify-backed fast watcher ("using WatchFiles" in logs)
  rather than the polling StatReload fallback.

- Migrate FastAPI @app.on_event("startup") → lifespan async
  context manager. Unified startup/shutdown scope; shutdown path
  now structurally impossible to forget. Verified graceful
  termination via `docker compose stop`: post-yield code runs,
  container exits 0 (not 143, not 137).
```
