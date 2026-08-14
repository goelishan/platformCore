-- Local buffer schema. Bootstrapped once by connection.bootstrap().
--
-- This file is a buffer, not the durable record. Everything written here is shipped to
-- the Postgres store and then becomes free to drop. The buffer exists so the write path
-- never depends on the network: the agent diagnoses broken clusters, and a collector
-- that cannot write when the cluster is unhealthy is a collector that fails exactly
-- when it is needed.
--
-- journal_mode is persistent (written into the file header). foreign_keys is NOT --
-- it is per-connection, defaults to OFF, and lives in the connection helper.

-- auto_vacuum comes FIRST, ahead of journal_mode, and the order is load-bearing.
-- Setting journal_mode writes the file header, and once page one exists auto_vacuum
-- can no longer be changed except by a full VACUUM. Reversed, this pragma is accepted
-- and silently does nothing — connection.bootstrap() verifies the result rather than
-- trusting it, because a silent no-op here surfaces months later as a buffer that
-- grows forever while its row count stays flat.
--
-- INCREMENTAL rather than FULL because this is a delete-heavy workload: a rolling
-- buffer drops rows continuously, and without it SQLite never returns freed pages to
-- the filesystem. The size backstop would then fire against space that is already
-- free. INCREMENTAL lets the sweep reclaim in bounded steps instead of one blocking
-- rewrite.
PRAGMA auto_vacuum = INCREMENTAL;

PRAGMA journal_mode = WAL;


CREATE TABLE IF NOT EXISTS collection_runs (
    run_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    cluster      TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,          -- ok | empty | unavailable
    signal_count INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_recent ON collection_runs(source, started_at);


CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    cluster     TEXT NOT NULL,
    namespace   TEXT,
    trigger     TEXT,                    -- alert name, or 'manual'
    title       TEXT,
    state       TEXT NOT NULL            -- open | diagnosed | resolved
);


-- blob_id is the sha256 of the bytes, and path is relative to BLOB_DIR. Relative,
-- because an absolute path is correct until the data directory moves between a laptop,
-- a PVC and a restored backup — after which every row asserts something false about
-- the local filesystem.
CREATE TABLE IF NOT EXISTS blobs (
    blob_id    TEXT PRIMARY KEY,
    path       TEXT NOT NULL,            -- raw logs live on disk, not in rows
    bytes      INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

-- Drives the age sweep, which is the only one that runs on a cadence.
CREATE INDEX IF NOT EXISTS idx_blob_expiry ON blobs(expires_at);
-- Drop order for the size backstop: oldest bytes first.
CREATE INDEX IF NOT EXISTS idx_blob_created ON blobs(created_at);


CREATE TABLE IF NOT EXISTS signals (
    -- observed by the collector
    signal_id     TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,         -- identity minus time: the recurring problem
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    event_time    TEXT NOT NULL,
    collected_at  TEXT NOT NULL,
    cluster       TEXT NOT NULL,
    namespace     TEXT,
    subject_kind  TEXT,
    subject_name  TEXT,
    subject_uid   TEXT,
    owner_kind    TEXT,                  -- joins a pod crash to the rollout behind it
    owner_name    TEXT,
    node_name     TEXT,                  -- separates "the app is broken" from "the node is"
    severity      TEXT,
    payload       TEXT NOT NULL,         -- JSON
    redacted      INTEGER NOT NULL DEFAULT 0,
    blob_id       TEXT REFERENCES blobs(blob_id),

    -- added by the landing zone
    run_id        TEXT REFERENCES collection_runs(run_id),
    incident_id   TEXT REFERENCES incidents(incident_id),
    expires_at    TEXT,                  -- store-side retention, not buffer-side

    -- Which version of envelope/template.py produced the key behind this fingerprint.
    -- Stamped here rather than carried on the envelope, because it describes the
    -- process doing the writing and not anything a collector observed -- and a
    -- collector able to set it is a collector able to get it wrong. Changing the mask
    -- rules re-keys some population of messages, so a weeks-old problem acquires a
    -- fresh fingerprint the day that change ships and first_seen_ever answers "never"
    -- with nothing raising. This column is what lets a query see that seam rather than
    -- walk into it.
    normalizer_version INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sig_corr
    ON signals(cluster, namespace, subject_name, event_time);
CREATE INDEX IF NOT EXISTS idx_sig_owner
    ON signals(cluster, namespace, owner_name, event_time);
CREATE INDEX IF NOT EXISTS idx_sig_recur
    ON signals(cluster, namespace, fingerprint, event_time);
-- No namespace: node-level failures cross namespace boundaries, and that is
-- exactly the pattern this index exists to surface.
CREATE INDEX IF NOT EXISTS idx_sig_node
    ON signals(cluster, node_name, event_time);
CREATE INDEX IF NOT EXISTS idx_sig_incident ON signals(incident_id);
-- Drives the buffer sweep: oldest-collected first, which is also drop order.
CREATE INDEX IF NOT EXISTS idx_sig_collected ON signals(collected_at);


CREATE TABLE IF NOT EXISTS diagnoses (
    diagnosis_id  TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL REFERENCES incidents(incident_id),
    created_at    TEXT NOT NULL,
    model         TEXT,
    bundle_sha256 TEXT,                  -- ties a verdict to exact evidence
    hypotheses    TEXT,                  -- JSON
    commands      TEXT,                  -- JSON
    input_tokens  INTEGER,
    output_tokens INTEGER,
    latency_ms    INTEGER
);


-- ---- shipping --------------------------------------------------------------
-- A transactional outbox. An entry is appended in the same transaction as the write it
-- describes, so the buffer can never hold a row the shipper does not know about, nor
-- promise a row that was rolled back.
--
-- The alternative — a shipped_at column on each table — loses data quietly. Rows here
-- are upserted on re-collection and updated again when incident_id is back-filled, so
-- a flag would have to be cleared correctly in every one of those paths, and one
-- missed path means a row that silently never ships. Appending is a single operation
-- that cannot be forgotten, and it is ordered.
--
-- AUTOINCREMENT, not a bare INTEGER PRIMARY KEY. Without it SQLite may reuse the
-- rowids of deleted rows, and this table is emptied continuously — a reused seq would
-- let the watermark move backwards.

CREATE TABLE IF NOT EXISTS outbox (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,             -- signal | incident | diagnosis
    row_id    TEXT NOT NULL,
    queued_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_row ON outbox(kind, row_id);


-- Same shape and same reason as collection_runs: a shipper that died quietly must not
-- be indistinguishable from a shipper with nothing to do.
CREATE TABLE IF NOT EXISTS shipping_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,           -- ok | empty | unavailable
    shipped     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_ship_recent ON shipping_runs(started_at);


-- The record of what the buffer gave up. The agent runs inside the cluster it
-- diagnoses, so an unbounded buffer eventually takes the agent down during the very
-- incident it exists to explain; dropping is the correct answer. Dropping *silently*
-- is not — a lost window would read downstream as a quiet period, which is the exact
-- failure this project is organised against. The assembler consults this table before
-- concluding anything from a gap.
CREATE TABLE IF NOT EXISTS buffer_drops (
    dropped_at   TEXT NOT NULL,
    reason       TEXT NOT NULL,          -- age | size
    rows_dropped INTEGER NOT NULL,
    unshipped    INTEGER NOT NULL DEFAULT 0,   -- of those, how many never reached the store
    window_start TEXT,
    window_end   TEXT
);

CREATE INDEX IF NOT EXISTS idx_drops_window ON buffer_drops(window_start, window_end);
