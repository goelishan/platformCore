-- Who asked for each diagnosis: manual (the UI or make diagnose) or auto (the agent's
-- triage loop).
--
-- The incident's trigger is not enough. The loop reuses an unresolved incident someone
-- opened by hand, so counting the loop's calls by incident would miss exactly those,
-- and its hourly spend cap would leak. It is also the split the evaluation reports by:
-- a diagnosis nobody asked for is read later and judged differently.
--
-- Nullable: NULL is a row from before this column.

ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS requested_by text;
