-- When the store first received each cluster's signals, written once and never moved.
--
-- watching_since was MIN(collected_at) over signals, and collected_at is rewritten on
-- every upsert of a row that is still changing. A cluster whose earliest rows were all
-- re-observed therefore appeared to have been watched only since the last poll, and every
-- bundle in that state lost its "is this new" claims to a blind note it did not need
-- (F5, drill 2026-10-13). A first-contact row is the horizon itself rather than a
-- reading of rows that keep moving.
--
-- The backfill takes the earliest collected_at that survives. That can only be later
-- than the true first contact, never earlier, so it errs towards "cannot tell" and never
-- towards calling an old problem new.

CREATE TABLE IF NOT EXISTS cluster_watch (
    cluster          text PRIMARY KEY,
    first_contact_at timestamptz NOT NULL
);

INSERT INTO cluster_watch (cluster, first_contact_at)
SELECT cluster, MIN(collected_at) FROM signals GROUP BY cluster
ON CONFLICT (cluster) DO NOTHING;
