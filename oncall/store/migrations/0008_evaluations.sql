-- Every diagnosis evaluated, and the evidence behind it kept beside it.
--
-- evaluations: one row per (diagnosis, evaluator), the buffer's shape. checks and outcome
-- are written by the agent, human by the engineer from the UI or make feedback. This is
-- what answers "how is the model doing on real incidents", which the frozen eval cannot:
-- fifteen lab faults say nothing about the ones production actually has.
--
-- bundle_snapshots: the bundle each diagnosis was made from, by evidence receipt. Until
-- now a snapshot lived only on the agent's volume, so on EKS a wrong diagnosis could be
-- read about but not replayed or turned into a regression case. Kept as long as the
-- diagnoses that cite it: they are the eval corpus, like signals attached to incidents.

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id     text PRIMARY KEY,
    diagnosis_id      text NOT NULL REFERENCES diagnoses (diagnosis_id),
    evaluator         text NOT NULL,
    evaluator_version text NOT NULL,
    created_at        timestamptz NOT NULL,
    verdict           text NOT NULL,
    score             double precision,
    cause_class       text,
    details           jsonb NOT NULL,
    notes             text
);

CREATE INDEX IF NOT EXISTS idx_eval_diagnosis ON evaluations (diagnosis_id);
CREATE INDEX IF NOT EXISTS idx_eval_evaluator ON evaluations (evaluator, verdict);

CREATE TABLE IF NOT EXISTS bundle_snapshots (
    bundle_sha256 text PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    bundle        jsonb NOT NULL
);
