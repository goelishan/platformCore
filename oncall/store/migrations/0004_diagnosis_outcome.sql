-- How each diagnosis attempt ended, not only what the successful ones said.
--
-- The journal is the eval corpus. A corpus that kept only successes could report
-- accuracy and never a failure rate: a model that answered malformed JSON half the time,
-- or proposed a write to the cluster, would look exactly as good as one that never did.
-- outcome, attempts and errors make every call count, including the ones that produced
-- nothing a reader could use.
--
-- prompt_version is the human-readable name for a prompt_sha256 group, so a report can
-- say "v2 against v1" without a reader matching hashes. cost_usd is computed from the
-- list prices configured at the time and stays NULL where none were, because an absent
-- figure and a zero cost are different claims.
--
-- Nullable throughout. Rows written before this migration were successes by
-- construction, and inventing an outcome for them would be worse than admitting the gap.
-- hypotheses, commands, the receipts, tokens and latency already exist and are reused.

ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS prompt_version text;
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS outcome text;
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS attempts integer;
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS stop_reason text;
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS cost_usd numeric(10, 6);
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS summary text;
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS errors jsonb;
ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS insufficient_evidence boolean;

-- Failure rate by prompt version is the question the eval asks most.
CREATE INDEX IF NOT EXISTS idx_diag_outcome ON diagnoses (prompt_version, outcome);
