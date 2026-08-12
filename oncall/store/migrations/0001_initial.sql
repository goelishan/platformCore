-- Durable store: initial schema.
--
-- Shape follows the buffer, types do not. The buffer is SQLite and stores everything as
-- text because it has to; the store has real types and uses them, which turns a class
-- of convention-enforced correctness into type-enforced correctness. iso() survives as
-- the *hashing* format for signal_id, not as the storage format.
--
-- Only what the shipper sends is here. collection_runs stays local: it describes the
-- health of the collectors attached to one buffer, and the queries that read it are
-- asked during an incident, when this store may be exactly what is unreachable.


CREATE TABLE IF NOT EXISTS incidents (
    incident_id text PRIMARY KEY,
    opened_at   timestamptz NOT NULL,
    closed_at   timestamptz,
    cluster     text NOT NULL,
    namespace   text,
    trigger     text,
    title       text,
    state       text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incident_open ON incidents (cluster, opened_at DESC);


-- ---- signals ---------------------------------------------------------------
-- Partitioned by event_time, monthly.
--
-- Retention here is the delete-heavy pattern that SQLite handles worst and Postgres
-- handles best: dropping a month becomes DROP TABLE on one partition, a metadata
-- operation, instead of a bulk DELETE that has to be vacuumed afterwards.
--
-- The primary key is composite because Postgres requires the partition key inside any
-- unique constraint on a partitioned table. It is not a widening of identity:
-- signal_id is a hash of fingerprint and event_time, so event_time is already
-- functionally determined by signal_id and the pair can never disagree.
--
-- blob_id carries no foreign key. Blobs are files, not rows — local disk today, object
-- storage later — so the reference is resolved by the blob store rather than by the
-- database, and shipping a signal must never depend on having shipped a blob.

CREATE TABLE IF NOT EXISTS signals (
    signal_id     text NOT NULL,
    fingerprint   text NOT NULL,
    source        text NOT NULL,
    kind          text NOT NULL,
    event_time    timestamptz NOT NULL,
    collected_at  timestamptz NOT NULL,
    cluster       text NOT NULL,
    namespace     text,
    subject_kind  text,
    subject_name  text,
    subject_uid   text,
    owner_kind    text,
    owner_name    text,
    node_name     text,
    severity      text,
    payload       jsonb NOT NULL,
    redacted      boolean NOT NULL DEFAULT false,
    blob_id       text,
    run_id        text,
    incident_id   text REFERENCES incidents (incident_id),
    expires_at    timestamptz,

    PRIMARY KEY (signal_id, event_time)
) PARTITION BY RANGE (event_time);


CREATE INDEX IF NOT EXISTS idx_sig_corr
    ON signals (cluster, namespace, subject_name, event_time);
CREATE INDEX IF NOT EXISTS idx_sig_owner
    ON signals (cluster, namespace, owner_name, event_time);
CREATE INDEX IF NOT EXISTS idx_sig_recur
    ON signals (cluster, namespace, fingerprint, event_time);

-- Deliberately cluster-wide and namespace-free: node failures cross namespace
-- boundaries, and that is exactly the pattern this index exists to surface.
CREATE INDEX IF NOT EXISTS idx_sig_node
    ON signals (cluster, node_name, event_time);

-- Drives first_seen_ever, the one question the buffer cannot answer. No cluster
-- column: "has this ever happened anywhere" is a stronger and more useful claim than
-- "has this ever happened here".
CREATE INDEX IF NOT EXISTS idx_sig_first_seen
    ON signals (fingerprint, event_time);

CREATE INDEX IF NOT EXISTS idx_sig_incident ON signals (incident_id);
CREATE INDEX IF NOT EXISTS idx_sig_expiry   ON signals (expires_at);

-- The reason payload is jsonb rather than text. In the buffer a query inside a payload
-- is a full scan; here it is an index lookup, which is what lets the assembler filter
-- on fields the schema never knew about — exit_code, event_uid, waiting_reason.
CREATE INDEX IF NOT EXISTS idx_sig_payload ON signals USING gin (payload);


CREATE TABLE IF NOT EXISTS diagnoses (
    diagnosis_id  text PRIMARY KEY,
    incident_id   text NOT NULL REFERENCES incidents (incident_id),
    created_at    timestamptz NOT NULL,
    model         text,
    bundle_sha256 text,
    hypotheses    jsonb,
    commands      jsonb,
    input_tokens  integer,
    output_tokens integer,
    latency_ms    integer
);

CREATE INDEX IF NOT EXISTS idx_diag_incident ON diagnoses (incident_id, created_at);


-- ---- partition management --------------------------------------------------
-- Lives in the database rather than in Python so that any writer gets it, including a
-- psql session during an incident. Idempotent, so callers can invoke it on every batch
-- without checking first.
--
-- Monthly, not daily: partition count is an operational cost of its own, and this
-- workload is measured in hundreds of rows a minute, not millions.

CREATE OR REPLACE FUNCTION ensure_signal_partition(at timestamptz)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    start_ts timestamptz := date_trunc('month', at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    end_ts   timestamptz := start_ts + interval '1 month';
    part     text := format('signals_%s', to_char(start_ts AT TIME ZONE 'UTC', 'YYYY_MM'));
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF signals FOR VALUES FROM (%L) TO (%L)',
        part, start_ts, end_ts
    );
    RETURN part;
END;
$$;
