-- What each diagnosis said it could not see.
--
-- The prompt requires the model to name, in unknowns, every gap in the evidence that
-- matters to its answer. Until now the journal dropped that list, so a stored diagnosis
-- read as more certain than the one the engineer was shown, and the eval corpus could
-- not measure whether blind sources were being acknowledged.
--
-- Nullable: NULL is a row from before this column, or an attempt that produced no
-- diagnosis; an empty array is a diagnosis that named no gap.

ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS unknowns jsonb;
