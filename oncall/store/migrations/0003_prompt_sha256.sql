-- The second receipt: which bytes the model was actually shown.
--
-- bundle_sha256 identifies the evidence that was selected. It is deliberately
-- independent of wording, so a reworded prompt template leaves it unchanged and a
-- diagnosis recorded last month stays comparable with one recorded today. That
-- independence is also a blind spot: a template edit that drops a whole section
-- leaves both receipts matching while the model sees strictly less, and the resulting
-- regression is attributed to the model.
--
-- One column cannot make both promises, so there are two. Nullable because every
-- diagnosis written before this migration was made against a prompt nobody hashed,
-- and inventing a value for those rows would be worse than admitting the gap.

ALTER TABLE diagnoses ADD COLUMN IF NOT EXISTS prompt_sha256 text;
