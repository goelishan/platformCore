-- Which version of the message normaliser produced the keys behind these fingerprints.
--
-- A fingerprint is a hash of the collector's dedupe_key, and for every collector that
-- keys on a message that key comes from envelope/template.py. Changing those rules
-- changes the key for some population of messages, so a problem that has recurred for
-- weeks acquires a new fingerprint the moment the change deploys. recurrences() then
-- reports a brand-new problem and first_seen_ever answers "never" -- confidently, and
-- with nothing raising anywhere.
--
-- The column does not prevent that. Nothing can: improving the normaliser and moving
-- the key are one event, not two. What it does is make the seam addressable, so a
-- query can compare fingerprints within a version and state how far back that version
-- actually reaches instead of implying history that was filed under a different hash.
--
-- Existing rows keep NULL, which reads correctly as "written before identity was
-- versioned". They are deliberately not recomputed: rewriting them would be expensive,
-- and it would alter evidence that past diagnoses were already hashed against, so the
-- eval corpus would stop matching what the reasoner actually saw.

ALTER TABLE signals ADD COLUMN IF NOT EXISTS normalizer_version integer;

-- Partial, because the whole point is grouping recurrences within one version and rows
-- from before versioning cannot participate in that.
CREATE INDEX IF NOT EXISTS idx_sig_recur_version
    ON signals (cluster, namespace, fingerprint, normalizer_version, event_time)
    WHERE normalizer_version IS NOT NULL;
