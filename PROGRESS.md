# PlatformCore — Progress Log

PlatformCore is the hands-on thread for Ishan's 40-day DBA → Platform Engineer transition. The full roadmap lives at [`docs/platform_engineer_40day_roadmap.html`](docs/platform_engineer_40day_roadmap.html).

**GitHub remote:** https://github.com/goelishan/platformCore

**This file is a safety net.** It is updated at the end of every work session so that progress is never lost — even if external tools or memory systems fail. `git log` is the second safety net. Memory files are the third.

---

## Current position

- **Phase:** 2 — Terraform IaC — **IN PROGRESS** as of 2026-04-21.
- **Day:** Day 6 completed and verified on 2026-04-21. Terraform pipeline bootstrapped end-to-end. Day 7 (VPC) begins next session.
- **Gate 2 deliverable (target):** PlatformCore infra fully in Terraform — VPC, subnets, SGs, EC2, ALB, ECR, IAM roles. Remote state in S3. Zero manual console steps.

## Open actions (for user to complete before/at next session)

- [x] Day 6 commit pushed to `main` on 2026-04-21 — Terraform bootstrap complete (provider.tf, backend.tf, variables.tf, outputs.tf, .terraform.lock.hcl committed; .terraform/ and state files gitignored).

## Day 7 agenda (tentative — confirm at session start)

- Rapid-fire round on Day 6 material (state file purpose + loss consequence, plan/apply lifecycle, remote state rationale, DynamoDB lock mechanism, data source vs resource distinction, `-out` flag race condition, lock file purpose, provider version constraints).
- Day 7: write the VPC resource. One `aws_vpc` resource, public + private subnets across two AZs, internet gateway, route tables. First chargeable-adjacent resource — confirm `terraform plan` shows correct diff before applying.

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

**Day 5 — Migration container + CI first pass**

- Added a one-shot `migrate` service using `postgres:16` (same image as the DB, so `psql` is available without a custom Dockerfile). It connects via the native PG* env vars (`PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`) and runs `psql -v ON_ERROR_STOP=1 -f /migrations/init.sql` against a read-only bind-mounted `./db/` directory. Restart policy is `on-failure:3` (finite job — exit 0 means done; retry up to 3x only on non-zero exit) as a deliberate contrast to `unless-stopped` on the long-running services. Without `ON_ERROR_STOP=1`, psql exits 0 even when statements inside the file fail — which would mean `on-failure` *never retries* and silent schema corruption ships to prod. Interview-correct combination: `ON_ERROR_STOP=1` + `on-failure:3` together, not separately.
- Created `db/init.sql` as an idempotent schema bootstrap: `CREATE TABLE IF NOT EXISTS app_version (...)` + `INSERT ... ON CONFLICT (version) DO NOTHING`, wrapped in `BEGIN;` / `COMMIT;`. Idempotency is a contract, not a nicety — the `on-failure:3` retry is only safe because re-running the migration produces identical state. Verified end-to-end: cold boot → postgres healthy → migrate runs + exits 0 → app starts; re-ran `docker compose restart migrate` and got `NOTICE: relation "app_version" already exists, skipping` + `INSERT 0 0` with no errors, proving idempotency. `SELECT * FROM app_version;` returned the `v0.1.0-phase1` row with timestamp `2026-04-20 12:13:05` as the persistence witness.
- Wired app's `depends_on` to gate on both `postgres: service_healthy` AND `migrate: service_completed_successfully`. The latter is the compose equivalent of K8s `initContainers` + `Job` resources: the main workload cannot start until the schema bootstrap has exited cleanly. Maps cleanly to Helm's `pre-install,pre-upgrade` hooks and Argo's `PreSync` waves.
- Added a GitHub Actions workflow at `.github/workflows/ci.yml`. Two jobs: `validate` runs `docker compose -f docker-compose.yml config` (base-only) and `docker compose config` (auto-merged with override) in sequence; `build` runs `docker compose build app` with `needs: validate` so broken YAML fails in seconds before burning CI minutes on image builds. `.env.example` is copied to `.env` inside the runner so compose's parse-time variable substitution resolves deterministically (without `.env`, substitutions produce empty strings and mask bugs — one of the classic CI gotchas). Verified the double-validate design by adversarial test before committing: appended a bogus YAML key to the override file, confirmed base-only validate still passed but merged validate failed as intended, then reverted. First CI run on `main` went green in 28 seconds (validate 6s, build 15s).
- Skipped the optional third compose file (`docker-compose.prod.yml`). Reasoning: in real production, the "prod overlay" lives outside the repo — Kubernetes manifests, Helm values per environment, Terraform-managed infrastructure — not as a third compose file checked in alongside dev. Materialising a third file here would have introduced a fiction that doesn't map to how production actually works. The base + override split is the compose-world fixture; everything beyond dev is the job of Phase 2+.

**Day 5 design story (interview-ready):**

Day 5 turned Phase 1's local stack into something closer to a real service pipeline. Database schema is no longer an implicit side effect of application startup — it's a distinct run-once job (`migrate`) with its own image, its own restart policy (`on-failure:3` because it's finite work, not a long-running service), and its own gate (`ON_ERROR_STOP=1` on psql so exit codes actually reflect SQL success). The migration is idempotent by construction, which is what makes the retry policy safe; without idempotency, `on-failure:3` becomes a schema-corruption multiplier. Application startup is now explicitly gated on `migrate: service_completed_successfully` — the same pattern K8s calls initContainers or Jobs, and that Helm ships as pre-install hooks. And CI is now a machine witness: every push is validated in both base-only and auto-merged compose modes (because override-only bugs can otherwise ship invisibly), and the app image build runs as a second job with an explicit `needs:` dependency, so cheap failures surface first. What would break it: a non-idempotent migration combined with `on-failure:3` would retry against partial state and corrupt the schema; an override-file typo would slip past a single-mode CI check and land in main; a forgotten `.env` in CI would silently resolve `${POSTGRES_USER}` to empty string and make compose config "succeed" against meaningless YAML. All three failure modes are fenced off today.

**Phase 1 closeout — interview narrative:**

PlatformCore's Phase 1 is a production-shaped, dev-friendly containerised stack behind a reverse proxy, with schema management, self-healing, and CI gating already in place. The topology is four services on two networks: nginx on `edge`, postgres on `backend`, the FastAPI `app` straddling both (the only service that needs to talk to the DB), and a one-shot `migrate` job on `backend` that runs before `app` starts. Defence-in-depth is enforced at the Docker network layer — nginx has no route to postgres at all, which is the compose-era equivalent of K8s NetworkPolicy or AWS SG chains. Secrets live in a root `.env` (gitignored); the YAML contains zero literal credentials. Every service has a healthcheck that tests its own primary process, not its upstream dependencies, which is how you avoid cascading false positives. Dev/prod concerns are split between `docker-compose.yml` (production-shaped base) and `docker-compose.override.yml` (dev-only: bind-mount, `--reload`, direct app port) — same architecture as Kustomize overlays or Helm values files, just using the tool we have. All long-running services have `restart: unless-stopped` for level-1 self-healing against organic crashes; the migration job has `on-failure:3` because it's finite work. FastAPI uses the modern `lifespan` context manager so shutdown is structurally impossible to forget — SIGTERM → post-`yield` cleanup → exit 0, all within K8s's default `terminationGracePeriodSeconds` of 30. CI gates every PR on compose syntax (validated twice, once base-only and once auto-merged, to catch override-only bugs) plus a successful image build. What Phase 1 does *not* include: multi-node scheduling, rolling updates, externally-managed infrastructure, observability beyond container logs, secret rotation, or anything above a single host — those are explicitly Phase 2+ concerns. What would break it first under load: a single postgres container means no replication or read-scaling; a host reboot evicts the OS page cache and causes a cold-cache performance cliff; stateful "pets" don't fit the "cattle" model that horizontal scaling requires. Each of those is a Phase-3 conversation (StatefulSets, read replicas, PgBouncer, pg_prewarm, operators), not a Phase-1 defect.

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

### 2026-04-20 — Session (continued, Day 5)

- Day 4 commit `7b90835` verified green on GitHub via UI. Session continued into Day 5.
- Ran rapid-fire round on day 4 material: 7/8 substantively clean. Clean: `restart: unless-stopped` vs `on-failure` semantics, `manuallyStopped` flag, override auto-merge, lifespan vs `on_event`, graceful shutdown exit codes, `terminationGracePeriodSeconds`. One gap — `watchfiles` vs polling `StatReload` — taught in real time. Pattern continues: structural intuition is ahead of mechanism detail, which is the correct direction for Platform Engineer interviews.
- Agenda picked: options A+B (CI workflow + migration container). Option C (explicit third `docker-compose.prod.yml`) skipped with sound reasoning — production overlays belong outside the compose toolchain (K8s manifests, Helm values, Terraform), so materialising a third compose file would have taught a fiction.
- Day 5 tasks shipped in order:
  - `db/init.sql` + `migrate` service in `docker-compose.yml`. Verified end-to-end: cold `docker compose down -v && up -d` → migrate exited 0 → app started → `SELECT * FROM app_version;` returned `v0.1.0-phase1`. Idempotency proven on `docker compose restart migrate`: `NOTICE: relation "app_version" already exists, skipping` + `INSERT 0 0`, no errors.
  - `.github/workflows/ci.yml` drafted with two jobs (validate + build, `needs:` dependency). Pre-commit gut-check experiment executed: appended bogus YAML key to override, confirmed base-only validate still passed while merged validate failed as designed. Damage reverted with `git checkout`. Design proven before commit.
  - Day 5 commit `8f7c448` pushed to `main`. First CI run green in 28 seconds: validate 6s, build 15s. Two advisory warnings about `actions/checkout@v4` embedding Node.js 20 (GitHub deprecating the Node 20 runtime for actions). Non-blocking; fix is to bump to `@v5` when convenient.
- Interview hooks banked this session: migration containers are the compose-era equivalent of K8s initContainers + Jobs and Helm pre-install hooks; idempotency is a contract that makes retry policies safe (non-idempotent + retry = corruption multiplier); `ON_ERROR_STOP=1` + `on-failure:3` are correct only together; CI jobs should order cheap checks (parse) before expensive checks (build) via explicit `needs:` DAG; validating both base-only and merged compose catches override-only bugs that would otherwise ship invisibly; actions are pinned like container images — `@v4` is the Node-world equivalent of `postgres:16`; adversarial testing of guards ("break the thing the check is supposed to catch") is the only way to trust a CI rule.
- Phase 1 closed. Gate 1 delivered with CI witness. Phase 1 closeout design story captured above. Session closed; Day 6 (Phase 2, Terraform) begins next session.

### 2026-04-21 — Session (Day 6, Phase 2 begins)

- Ran rapid-fire round on Day 5 material: 4.5/6. Clean: idempotency reasoning, dependency chaining logic, override-specific bug class. Gaps addressed: exact exit-code mechanism for `ON_ERROR_STOP=1` (psql exits non-zero → container exits non-zero → `on-failure` triggers; without the flag psql exits 0 silently); "false green" framing for missing `.env` in CI (compose config succeeds but validates against empty strings — silent pass, not a visible failure); provider/action pinning analogy (both are dependency version locks — upstream changes arrive on your schedule, not theirs). Second half of the double-validate question taught: a bug in the base file that the override masks — override overrides the broken key, merged passes, base-only fails. Real scenario: someone patches a broken base config via the override instead of fixing the base directly.
- Phase 2 setup: installed AWS CLI via official pkg installer (Homebrew formula broken on Python 3.14/libexpat incompatibility). Created `platformcore-terraform` IAM user with AdministratorAccess; generated access keys; configured `aws configure` on Mac. Verified `aws sts get-caller-identity` returns `platformcore-terraform` user (not root).
- Bootstrapped remote backend via AWS CLI (one-time manual step — the chicken-and-egg solution): S3 bucket `platformcore-tf-state` with versioning enabled and public access blocked; DynamoDB table `platformcore-tf-locks` with `LockID` partition key, PAY_PER_REQUEST billing.
- Created `terraform/` directory in repo with four files: `provider.tf` (hashicorp/aws ~> 5.0, required_version >= 1.9), `backend.tf` (S3 + DynamoDB remote state, encrypt=true), `variables.tf` (aws_region, default us-east-1), `outputs.tf` (account_id from aws_caller_identity data source).
- `terraform init` clean: backend configured to S3, provider hashicorp/aws v5.100.0 installed, `.terraform.lock.hcl` generated.
- `terraform plan` and `terraform apply` both clean: state lock acquired/released via DynamoDB, data source read, account_id output surfaced. Zero chargeable resources created.
- `.gitignore` updated: `.terraform/` and state files excluded; `.terraform.lock.hcl` committed (provider version lock — equivalent to package-lock.json).
- Day 6 commit pushed to `main` on 2026-04-21.

**Interview hooks banked this session:**
- State file = Terraform's memory. Losing it means Terraform doesn't know what it manages — can't modify or destroy existing resources. Disaster recovery = S3 versioning (roll back to last known-good state file).
- DynamoDB lock = mutex on state. Prevents concurrent applies from corrupting state. Orphaned lock (crashed apply) requires manual DynamoDB item deletion to unblock.
- Data source vs resource: resources are lifecycle-owned (create/modify/destroy); data sources are read-only queries — import facts about existing infra without taking ownership.
- `terraform plan -out=tfplan` + `terraform apply tfplan` as atomic pair — prevents race condition where infra changes between plan approval and apply execution. Standard in production CI (Atlantis, Terraform Cloud, GHA).
- Backend block cannot use variables — evaluated before variable resolution. Backend config must use hardcoded literals.
- Provider version constraint `~> 5.0` = "5.x but not 6.x". Major version bumps require explicit decision; minor version upgrades are automatic. Same principle as `postgres:16` image pinning.
- Root credentials (`arn:aws:iam::ACCOUNT:root`) should never be used for day-to-day work — created dedicated IAM user with AdministratorAccess for Terraform (to be scoped to least-privilege when IAM-via-Terraform is built in Phase 2).
- Outputs are how Terraform modules compose — VPC module outputs `vpc_id`; compute module takes it as input variable. Pattern used heavily from Day 7 onward.

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
