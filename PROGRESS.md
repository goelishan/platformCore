# PlatformCore — Progress Log

PlatformCore is the hands-on thread for Ishan's 40-day DBA → Platform Engineer transition. The full roadmap lives at [`docs/platform_engineer_40day_roadmap.html`](docs/platform_engineer_40day_roadmap.html).

**GitHub remote:** https://github.com/goelishan/platformCore

**This file is a safety net.** It is updated at the end of every work session so that progress is never lost — even if external tools or memory systems fail. `git log` is the second safety net. Memory files are the third.

---

## Current position

- **Phase:** 2 — Terraform IaC — **IN PROGRESS** as of 2026-04-25.
- **Day:** Day 9 **CLOSED and VERIFIED** on 2026-04-25. Full deployment path proven end-to-end: ALB serves 200 from a container running on a private-subnet EC2 with no NAT gateway; image pulled from ECR through ecr.api + ecr.dkr + s3 gateway endpoints; logs stream to CloudWatch via the logs interface endpoint. `make curl` returns 200 on `/`, `/health`, `/version`, and 503 (expected) on `/ready`. Target group state: `healthy`. Day 10 (likely RDS or GitHub Actions OIDC; check roadmap) begins next session.
- **Gate 2 deliverable (target):** PlatformCore infra fully in Terraform — VPC, subnets, SGs, EC2, ALB, ECR, IAM roles. Remote state in S3. Zero manual console steps.

## Open actions (for user to complete before/at next session)

- [x] Day 7 commit pushed to `main` on 2026-04-21 — VPC, subnets, IGW, route tables, outputs all committed.
- [ ] Day 8 commit — SGs, IAM, EC2, ALB, default_tags, Makefile, headers, tag audit.
- [ ] Day 9 commit — VPC endpoints (SSM trio + ECR.api + ECR.dkr + S3 gateway + CloudWatch Logs), private RT, ECR repo + lifecycle, IAM (ECR readonly + scoped Logs-write), user_data for boot-time pull/run, lazy-DB main.py, Makefile endpoint teardown.
- [ ] AWS Budgets `$5/mo` alert (`budgets.tf`) — second layer of cost defence beyond the Makefile.
- [ ] Rapid-fire recall round on Day 8 material (still pending).
- [ ] Final Day 9 verification once `:v2` deployed: `make curl` returns 200 on `/`, `/health`, `/version`; `/ready` returns 503 (expected, no DB).

## Day 9 agenda (tentative — confirm at session start)

- Rapid-fire on Day 8: source-SG reference pattern, standalone rule resources vs inline, IAM role vs instance profile, IMDSv2 mechanism, AMI data source semantics, ALB vs NLB, target group health-check independence from app routes, `-target` tradeoffs, `default_tags` vs explicit tags, 502 as a healthy ALB signal.
- Day 9: deploy actual app code to the EC2 so the target group turns healthy and the ALB serves 200s. Likely involves user_data or AMI bake decision, plus the NAT Gateway / VPC endpoint question (private subnet has no outbound path today — `yum install` / ECR pulls will fail without it).

## Day 9 in-flight notes (2026-04-23)

**Architecture decision: Option A — VPC Endpoints, no NAT.** Private subnet reaches AWS services via per-service endpoints. Cheaper than NAT gateway at this scale and safer (AWS-backbone-only traffic). Tradeoff: one endpoint per service, and interface endpoints are the biggest cost leak if left up overnight (~$0.01/hr each per AZ) — `make down` now tears them all down.

**Endpoints provisioned:** SSM trio (`ssm`, `ssmmessages`, `ec2messages`), ECR pair (`ecr.api` control, `ecr.dkr` data), S3 gateway (ECR's layer storage backend), CloudWatch Logs. STS deliberately omitted — the app doesn't call `sts:GetCallerIdentity`; `get-login-password` uses instance-profile credentials directly via SigV4 without identity lookups.

**Bootstrap trap (resolved):** Plain AL2023 needs `dnf install docker` at first boot, but AL2023 package repos are on CloudFront (public internet) and there is no VPC endpoint for them. Switched to ECS-optimized AL2023 AMI, which ships Docker pre-baked. Happy accident: `most_recent=true` + a loose AMI name filter (`al2023-ami-*-x86_64`) had already picked the ECS-optimized image — tightening the filter (`al2023-ami-ecs-hvm-*-x86_64`) returned the same AMI with zero drift. Banked as an interview lesson: **loose filter + most_recent = hidden drift vector; pin the exact prefix.** Also disabled the systemd-managed ECS agent (`systemctl disable ecs`) on the running instance during manual verification to stop it crash-looping against our non-existent cluster.

**Network-path proof points (what the `:v1` crash taught us):**
1. `aws ecr get-login-password` from the EC2 returns `AccessDenied` → confirms ecr.api endpoint + SigV4 reach AWS; only IAM needs fixing. After policy attach, token returned.
2. `docker pull` downloads all 8 layers → confirms ecr.dkr endpoint (manifest) + S3 gateway (blobs) both work.
3. App crash-logs arrive in CloudWatch within seconds → confirms awslogs driver + logs endpoint + scoped IAM policy all work.

**IAM shape (Phase 9 additions):**
- `AmazonEC2ContainerRegistryReadOnly` (AWS-managed, attached) — ECR pull + token.
- `platformcore-cw-logs-write` (inline, scoped to `/platformcore/*` log groups) — `CreateLogStream`, `PutLogEvents`, `CreateLogGroup`, `DescribeLogStreams`. Scoped, not the broader `CloudWatchAgentServerPolicy`.

**Three-resource IAM triangle banked:** `aws_iam_role_policy_attachment` (managed policy → role), `aws_iam_role_policy` (inline), `aws_iam_policy + attachment` (customer-managed reusable). All three used intentionally in `iam.tf`.

**App refactor (Path B, committed in this session):** `app/main.py` rewritten to be lazy about the DB. New endpoint shape:
- `/` — DB-free, smoke test.
- `/health` — liveness. DB-free by design. ALB target group health check points here.
- `/ready` — readiness. Touches DB via `SELECT 1`. Returns 503 in Phase 9 (no RDS). Will be the ALB health target after Phase 10.
- `/version` — returns `APP_VERSION` env var (injected in user_data to match the pulled image tag).

**Liveness vs readiness mental model banked:** liveness checks the process, readiness checks dependencies. Wiring a health check that hits the DB turns a DB blip into a container restart-loop — strictly worse than a brief removal from LB rotation. Production setups split the two; our Phase 9 setup has the split ready for Phase 10 to activate.

**Final verification — completed 2026-04-25:**
- Built `:v2` with `docker buildx build --platform linux/amd64` from Apple Silicon laptop.
- Pushed to ECR; user_data pinned to `:v2` via `local.app_image_tag`.
- First instance after `make up` failed `docker pull` because `:v2` had not yet been pushed at apply time — `set -e` killed user_data after the start marker, before the run line. **Lesson banked: push image BEFORE the apply that depends on it.**
- Recovered without instance replacement by SSM-shelling in and pasting the failed-tail of user_data manually. Proves the user_data text is correct; only the ordering broke.
- Hit a second IAM gap during recovery: `logs:PutRetentionPolicy` was not in the inline policy. The `|| true` in user_data swallowed the error so the script continued. Retention not yet set on the log group; tracked as cleanup work for next session (likely move log-group management out of user_data and into Terraform via `aws_cloudwatch_log_group` + `terraform import`).
- Final state: ALB target `healthy`, all four endpoints respond as expected, container shows `Up X seconds` with no crash loop.

## Day 9 follow-up tasks (carry into Day 10 opener or end-of-day cleanup)

- [ ] Move CloudWatch log group to Terraform (`aws_cloudwatch_log_group.app` with `retention_in_days = 7`); `terraform import` the existing group; remove `aws logs create-log-group` and `aws logs put-retention-policy` from user_data.
- [ ] Tighten IAM Logs policy to just `CreateLogStream` + `PutLogEvents` once the log group is Terraform-managed. The awslogs Docker driver needs nothing else.
- [ ] AWS Budgets `$5/mo` alert (`budgets.tf`) — open since Day 8.
- [ ] Day 8 + Day 9 git commits (separate, both with detailed messages).
- [ ] Permanent fix to suppress the systemd-managed `ecs-agent` container on the ECS-optimized AMI — added to user_data; will land on next instance replacement.

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

### 2026-04-21 — Session (Day 7, VPC)

- Rapid-fire on Day 6 material: 4.5/6. Gaps corrected: state file is a record of what Terraform has already created (not instructions for future work) — losing it orphans existing resources and causes Terraform to try creating duplicates on next apply; `-out` race condition is between your own plan and apply (AWS changes in between), not between two engineers; S3 versioning on state bucket is the disaster recovery mechanism for state corruption.
- Built full VPC networking in `terraform/vpc.tf`:
  - `aws_vpc.main`: 10.0.0.0/16, DNS hostnames + support enabled
  - `data.aws_availability_zones.available`: dynamic AZ query (no hardcoded us-east-1a/b)
  - `aws_subnet.public` x2: 10.0.0.0/24 + 10.0.1.0/24, us-east-1a/b, `map_public_ip_on_launch=true`
  - `aws_subnet.private` x2: 10.0.10.0/24 + 10.0.11.0/24, us-east-1a/b
  - `aws_internet_gateway.main`: attached to VPC
  - `aws_route_table.public`: single route 0.0.0.0/0 → IGW
  - `aws_route_table_association.public` x2: binds each public subnet to the public route table
- `outputs.tf` updated: vpc_id, public_subnet_ids, private_subnet_ids (using `[*]` splat)
- `terraform plan -out=tfplan` + `terraform apply "tfplan"` used correctly throughout — no re-plan race condition
- Taught DNS fundamentals: what DNS is, resolution hierarchy, TTL, what breaks when it fails (everything that uses names, which is everything), connection to Phase 1 musl/IPv6 bug
- Taught VPC traffic flow with ASCII diagram: route table entry `0.0.0.0/0 → IGW` is the single structural difference between public and private subnet; private subnets are currently internet-dead (outbound) — NAT Gateway needed for app servers to reach internet (future day)
- Taught Terraform resource dependency graph: references between resources (`gateway_id = aws_internet_gateway.main.id`) are both value lookups and implicit dependency declarations — Terraform derives creation order and parallelises independent resources automatically
- cidrsubnet corrections: `cidrsubnet("10.0.0.0/16", 8, N)` produces /24 subnets; `+10` offset for private subnets is a readability convention, not an AWS reservation requirement

**Interview hooks banked this session:**
- Route table `0.0.0.0/0 → IGW` is the only thing that makes a subnet "public" — not the subnet itself, not the VPC, just that one route entry. Remove it and the subnet is effectively private.
- Route table association is the binding step — without it, a subnet falls back to the VPC default route table (no IGW route), silently behaving as private even if intended to be public.
- `map_public_ip_on_launch=true` on public subnets: EC2 instances get a public IP automatically. Private subnets: no public IP, no inbound internet path, no outbound internet path (until NAT GW).
- Terraform dependency graph: resource references = implicit dependencies. Terraform parallelises all resources with no dependency relationship. You never write ordering — you write references and let Terraform derive the DAG.
- Dynamic AZ data source over hardcoded AZ names: if an AZ goes into maintenance, Terraform picks a healthy one. Hardcoded names create silent single-AZ risk if that AZ is unavailable.
- Two AZs minimum for any production workload: single-AZ means one AWS hardware failure takes down the entire service. Two AZs = fault tolerance at the infrastructure layer.
- `[*]` splat expression: `aws_subnet.public[*].id` returns a list of all IDs from a count-based resource — the standard pattern for passing subnet lists to downstream modules (ALB, EKS, RDS).
- Private subnet outbound dead end today: no `0.0.0.0/0` route = packet dropped. App servers in private subnets can't pull images, call APIs, or reach AWS services without NAT Gateway. NAT GW sits in public subnet, has Elastic IP, allows outbound-initiated traffic from private subnets while blocking inbound. Coming in a future day.

- Day 7 commit pushed to `main` on 2026-04-21.

### 2026-04-22 — Session (Day 8, Security Groups + EC2 + ALB)

- Rapid-fire on Day 7 material: 6.5/8. Clean: subnet public/private definition (route table entry is the single structural difference), route table association purpose, `map_public_ip_on_launch` semantics, IGW as VPC-border NAT for 1:1 public-IP mapping, dynamic AZ data source over hardcoded names, outputs as module interface, two-AZ minimum reasoning. Gap: `[*]` splat confused with `count`/`for_each` — corrected in real time. Splat is about *shape* (extract one attribute across a list → flat list), not *scale* (count/for_each control how many resources get created). They compose: count creates the list, splat flattens one attribute off it. Second gap: user's framing of instance profile as "allows deeper configuration" — corrected to "thin wrapper EC2's legacy RunInstances API requires; IAM model is `role + policy`; the profile is the plumbing EC2 needs to actually receive the role at launch."
- Built a scenario-based drill deck before shipping code: `docs/phase2_scenario_drills.md` — 12 scenarios across the Phase 2 surface area (SGs, IAM, ALB, state backend, cost). Each scenario has the five-prompt framework (Fit / Doesn't fit / Better / Scale / Failure modes) + collapsible scaffold + trap warning. Purpose: build *finding the trap* as a meta-skill for interviews. Trap classes identified: false-scale (looks elastic, isn't), false-security (looks hardened, has a gap), false-simplicity (looks clean, hides coupling), false-economy (looks cheap, scales badly).
- Day 8 resources shipped:
  - `security_groups.tf`: two SGs (`alb_sg` public-facing on 80, `ec2_sg` private app on 8000) + four standalone rule resources (`aws_vpc_security_group_ingress_rule` / `aws_vpc_security_group_egress_rule`). Chose standalone rules deliberately: inline rules would have created a circular dependency — `ec2_sg` references `alb_sg` as its ingress source, but if `alb_sg` had an inline egress rule pointing at `ec2_sg`, Terraform's DAG would deadlock. User diagnosed this structurally before writing the code ("at the time of creation ALB will ask for ec2 sg id, which is not deployed yet"). Keystone pattern shipped: `ec2_sg` ingress on 8000 with `referenced_security_group_id = aws_security_group.alb_sg.id` — **identity-based authz, not CIDR-based** — IP-independent, survives ALB recreation, encodes "only traffic whose source SG is alb_sg" as the rule itself.
  - `iam.tf`: IAM role with EC2 trust policy via `data.aws_iam_policy_document` (type-safe JSON assembly vs `jsonencode` string-building), `AmazonSSMManagedInstanceCore` managed policy attachment, `aws_iam_instance_profile` wrapper. User initially wrote `resource "aws_iam_role.ec2_ssm"` (single string with dot) — corrected to the two-label form `resource "aws_iam_role" "ec2_ssm"`. Also had typo `resouce`, an invalid `attach_policy` argument, an unquoted ARN. Taught the habit: run `terraform fmt && terraform validate` before asking for review — the machine catches these instantly and you don't burn a code-review round on syntax.
  - `ec2.tf`: `data.aws_ami.al2023` with `most_recent = true` + owner filter (`["amazon"]`) — dynamic lookup pulls the latest patched AL2023 x86_64 image; pinning an ID creates silent stale-image risk across regions. Instance placed in `aws_subnet.private[0]`, `associate_public_ip_address = false`, SSH explicitly absent (no `key_name`, no port 22 anywhere in the SG chain). Shell access via SSM Session Manager using the instance profile. `metadata_options { http_tokens = "required" }` enforces IMDSv2 — prevents SSRF attacks against `169.254.169.254` from stealing the attached IAM role's temporary credentials. `root_block_device` gp3, 30 GB (AL2023 AMI's minimum snapshot size; 20 GB was rejected by RunInstances on first apply), `encrypted = true` via AWS-managed EBS KMS key.
  - `alb.tf`: `aws_lb` (type=application, `subnets = aws_subnet.public[*].id`, multi-AZ by construction), `aws_lb_target_group` on port 8000 with `/health` health check + HTTP 200 matcher, `aws_lb_target_group_attachment` binding the EC2, `aws_lb_listener` on port 80 with a default `forward` action. Target group health check is decoupled from `/` — means `/` can redirect, 500, or be rate-limited without marking the target unhealthy. ALB is L7 (path/host/header routing, HTTP-aware); NLB would be L4 (raw TCP, no content inspection). Chose ALB because we'll add path-based routing and HTTPS termination in later phases.
  - `outputs.tf`: added `app_instance_id`, `ec2_ssm_role_arn`, `alb_dns_name`, `alb_url`.
  - `provider.tf`: `default_tags { tags = { Project = var.project_name; ManagedBy = "terraform" } }` — applied to every resource that supports tags. Explicit `Name` + `Environment` tags kept on resources where they vary. DRY without losing specificity.
  - Full-file header comment blocks added to `variables.tf`, `backend.tf`, `main.tf`, `vpc.tf`, `security_groups.tf`, `iam.tf`, `ec2.tf`, `alb.tf` — each explains *why* the file exists and the non-obvious design choices it encodes, not *what* the resources do (code already says that).
- Bugs caught / interview-grade lessons:
  - **Circular SG dependency**: solved with standalone rule resources — AWS provider v5+ idiom for exactly this case.
  - **Em-dashes (`—`) in SG descriptions**: AWS's `CreateSecurityGroup` API rejects non-ASCII characters per its documented allow-list `^[\x00-\x7F]*$`. Fixed by replacing three em-dashes with hyphens. Interview lesson: AWS APIs have per-field character allow-lists; don't trust Unicode to round-trip.
  - **Volume size 20 GB < AL2023 minimum 30 GB**: my mistake, caught by `RunInstances`. Fix: bump to 30, or reference `data.aws_ami.al2023.block_device_mappings[0].ebs.volume_size` dynamically.
  - **Partial apply recovery**: ALB, target group, listener, attachment all created before EC2 failed. No state surgery — re-running apply just reconciled the missing instance. State is diff-driven; partial applies are recoverable by design.
- Cost-control layer shipped:
  - `Makefile` at project root with four targets: `up` (full `terraform apply`), `down` (`terraform destroy -target` on listener + attachment + target group + ALB + EC2 only — keeps VPC/SGs/IAM which cost $0), `rebuild` (down→up), `status` (`terraform state list`).
  - Rationale: ALB is ~$16/mo 24/7, EC2 t3.micro ~$7.50/mo, EBS gp3 30 GB ~$2.40/mo = ~$26/mo if left running; ~2 hrs/day brings it to ~$2-3/mo. Daily teardown habit saves ~90%.
  - `-target` is pragmatic not idiomatic: HashiCorp explicitly discourages it for routine use because it bypasses the DAG. Interview framing: "I use it locally for cost-driven teardown; production answer is separate stacks, or full teardown + recreate, or Terragrunt-modelled lifecycle."
  - **Makefile gotchas encountered:** (1) recipe lines must start with literal TAB — editors auto-converting to spaces caused `missing separator. Stop.` (2) Makefile initially placed in `terraform/` but its recipes used `cd terraform && ...`, which breaks when run from inside `terraform/`. Moved to project root; removed `cd` prefix mismatch. Teaching moment: relative-path mental model (`./`, `../`, cwd-relative) shows up everywhere — Terraform module sources, Docker build contexts, `kubectl apply -f`.
  - Teardown path verified end-to-end on 2026-04-22 — `make down` removed the five billable resources cleanly, `make status` showed only the free ones remaining.
- Housekeeping: teardown proven, progress file updated, recap delivered to user for review.

**Interview hooks banked this session (Day 8):**

- Security groups are stateful L4 firewalls at the ENI level — return traffic is allowed automatically, never write egress rules for response packets. Compare NACLs (stateless, subnet-level, numbered precedence) as the deliberate-contrast second layer.
- Source-SG reference (`referenced_security_group_id`) is identity-based authorization: "allow traffic whose source SG is X" — IP-independent, survives ALB recreation, and is the pattern you want over CIDR allow-lists whenever the source is another AWS resource in the same VPC. CIDRs are for external sources (office IPs, VPN ranges, partners).
- Standalone rule resources (`aws_vpc_security_group_ingress_rule`) break circular dependencies in the Terraform DAG that inline rules would create. General pattern: when two resources cross-reference each other, the edges that encode the relationship must be nodes the DAG can topologically sort — separate them into their own resources.
- IAM role vs instance profile: the role is the principal + policy attachment. The instance profile is a thin EC2-specific passthrough required by the legacy `RunInstances` API — EC2 can't attach a role directly, only an instance profile that wraps one. It's plumbing, not a richer abstraction. Lambda/ECS/etc. attach roles directly; EC2 is the odd one out because the profile concept predates the modern IAM model.
- SSM Session Manager replaces SSH: no key pair, no port 22, no bastion host. Shell over the AWS API, gated by IAM (`ssm:StartSession`). `AmazonSSMManagedInstanceCore` managed policy on the EC2 role + SSM agent on the AMI (AL2023 ships with it) = done. Audit trail: every session logged in CloudTrail + optionally full keystroke log to S3 / CloudWatch.
- IMDSv2 (`http_tokens = "required"`): forces token-based requests to `169.254.169.254`. IMDSv1 is vulnerable to SSRF — if the app has any URL-fetching endpoint, an attacker can trick it into reading the metadata service and exfiltrating the EC2 role's temporary credentials. Capital One breach (2019) is the canonical citation. Mandatory hardening for anything customer-facing.
- AMI data source with `most_recent = true` + owner filter: always pulls the latest patched AL2023. Pinning an ID gives you determinism but creates stale-image risk — you'll ship a CVE-vulnerable base months after Amazon patched it. Production answer is a base-image pipeline (Image Builder / Packer) that bakes your org's hardened baseline on a scheduled rebuild, pinned to that output.
- ALB vs NLB: L7 vs L4. ALB terminates HTTP, inspects headers/paths/hosts, does WebSocket, supports HTTPS termination + ACM integration + WAF attachment, hands traffic off as HTTP to targets. NLB terminates TCP/UDP at L4, preserves source IP, handles millions of req/s, does HTTPS passthrough. Rule of thumb: HTTP routing → ALB; raw TCP / high-throughput / source-IP preservation / non-HTTP protocols → NLB.
- Target group health checks are decoupled from the app's routing surface. `/health` is a dedicated endpoint that tests only the app's liveness — not the DB, not upstream services. If `/health` tests downstream dependencies, a transient DB blip marks *every* target unhealthy and drains the entire fleet simultaneously. Same lesson as Day 3's nginx healthcheck: test your own primary process, not your dependencies.
- ALB returns **502 Bad Gateway** when no target is healthy. That's not a failure of the ALB — it's the correct protocol-level signal that the load balancer is reachable but has nothing to route to. Seeing the 502 end-to-end is the bridge moment from "infra exists" to "app needs to exist."
- `terraform destroy -target` is the escape hatch for cost-driven local iteration. HashiCorp docs call it "advanced" and discourage routine use because it bypasses the dependency graph — the `down` target still works only because we carefully listed all five billable resources in the correct teardown order. Production answer is a separate stack or full teardown.
- Provider `default_tags` block is the DRY pattern for tags every resource must have (Project, ManagedBy, CostCenter). Explicit tags per resource for what varies (Name, Environment on resources that span environments). Tag consistency pays off at billing time — `Project=platformcore` lets you filter Cost Explorer to just this stack without resource-by-resource accounting.
- AWS API character allow-lists: per-field, per-service. SG descriptions are ASCII-only (`^[\x00-\x7F]*$`); S3 bucket names are lowercase DNS-safe; IAM role names have their own set. Never assume Unicode round-trips through an AWS API — copy-paste from editors that smart-quote or em-dash can silently break an apply.
- Makefile recipe indentation is *literal tab* — not "tab-width spaces." Many modern editors default to space-expansion, which silently converts a valid Makefile to an invalid one. `.editorconfig` with `[Makefile] indent_style = tab` is the fix.
- Cost-control interview framing: "For a learning/dev environment I tear down billable resources nightly via a Makefile with `-target`; in production I'd solve it at the stack boundary — separate ephemeral preview environments that spin up per-PR via CI and tear down on merge, running on spot-priced compute, with AWS Budgets alerts as a second-layer backstop on the bill itself."

**Day 8 design story (interview-ready):**

Day 8 converted PlatformCore from "a network" into "a service." The ALB is the public entry point in the public subnets; the EC2 is the workload in a private subnet with zero inbound path from the internet; the two are connected only through security group references, not CIDRs, so the authorization rule is identity-based and survives resource recreation. SSH is explicitly absent — shell access is over the AWS API via SSM Session Manager, gated by IAM, fully audited in CloudTrail, with no port 22 in any SG and no key pair attached to the instance. IMDSv2 is required, which closes the SSRF-to-credential-theft class of exploits that Capital One paid $190M to learn about. The root volume is encrypted at rest. The AMI is pulled dynamically so security patches arrive on Amazon's schedule rather than whenever we happen to remember to update a hardcoded ID. Provider `default_tags` give every resource Project/ManagedBy labels for cost allocation; explicit Name/Environment tags handle the stuff that varies. A Makefile at the repo root wraps four operating verbs (`up`, `down`, `rebuild`, `status`); the `down` target uses `terraform destroy -target` to tear down only billable resources (ALB, target group, listener, attachment, EC2) while leaving the free tier (VPC, subnets, route tables, SGs, IAM role + instance profile) intact, cutting idle cost by ~90% for a learning environment that runs ~2 hours a day. What would break it: the wide-open egress on `ec2_sg` (0.0.0.0/0) is a noted tech-debt — in production that becomes an allowlist of just the endpoints the app actually needs (ECR, S3, Secrets Manager, VPC endpoints); the ALB listener is HTTP-only, which is Day-9+ work (ACM cert + HTTPS listener + HTTP→HTTPS redirect); the private subnet has no outbound internet path today, so any app that needs to pull packages or call external APIs will fail until NAT Gateway or VPC endpoints arrive; and `-target` destroys are interview-pragmatic but production-wrong — the real answer is ephemeral per-PR stacks, spot pricing, and AWS Budgets as a bill-level backstop.

**Interview hooks banked this session (Day 6):**
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
