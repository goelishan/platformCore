-- db/init.sql — PlatformCore initial schema.
--
-- Idempotent by design: safe to re-run on every `docker compose up`.
--   - CREATE TABLE IF NOT EXISTS  → no error if table already exists
--   - INSERT ... ON CONFLICT DO NOTHING  → no error if row already exists
--
-- The migration container runs psql with ON_ERROR_STOP=1, so any SQL
-- error here (typo, constraint violation, missing extension) will exit
-- non-zero and trigger the on-failure:3 retry. Fail loudly, fail fast.

BEGIN;

CREATE TABLE IF NOT EXISTS app_version (
  version    TEXT        PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO app_version (version)
VALUES ('v0.1.0-phase1')
ON CONFLICT (version) DO NOTHING;

COMMIT;