-- Landing zone schema. Bootstrapped once by connection.bootstrap().
-- journal_mode is persistent (written into the file header). foreign_keys is NOT —
-- it is per-connection, defaults to OFF, and lives in the connection helper.

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


CREATE TABLE IF NOT EXISTS blobs (
    blob_id    TEXT PRIMARY KEY,
    path       TEXT NOT NULL,            -- raw logs live on disk, not in rows
    bytes      INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT
);


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
    expires_at    TEXT
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
CREATE INDEX IF NOT EXISTS idx_sig_expiry   ON signals(expires_at);


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