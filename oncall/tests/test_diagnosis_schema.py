"""
The output contract rejects what the reasoner must never journal as a diagnosis.

Each rejection test starts from a valid diagnosis and changes one thing, so a failure
names the rule that broke rather than whichever field happened to be wrong first.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oncall.diagnosis.schema import (
    MAX_CITATIONS,
    MAX_SUMMARY_CHARS,
    CauseClass,
    Diagnosis,
    DiagnosisRecord,
    Outcome,
    tool_input_schema,
)

FP_A = "3f9c2a7be41d0c5a9e8f7b6a5c4d3e2f"
FP_B = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _hypothesis(rank: int, cause: str = "image_pull") -> dict:
    return {
        "rank": rank,
        "cause_class": cause,
        "title": "New ReplicaSet cannot pull its image",
        "mechanism": "registry.invalid does not resolve, so the new pod never starts.",
        "confidence": "high",
        "supporting": [FP_A],
        "contradicting": [],
        "verify": [{"command": "kubectl -n oncall-lab describe pod x", "purpose": "events"}],
    }


VALID = {
    "summary": "A rollout is blocked by an image that cannot be pulled.",
    "hypotheses": [_hypothesis(1), _hypothesis(2, "other")],
    "unknowns": [],
    "insufficient_evidence": False,
}


def _with(**changes) -> dict:
    data = copy.deepcopy(VALID)
    data.update(changes)
    return data


def test_a_valid_diagnosis_parses():
    d = Diagnosis.model_validate(VALID)

    assert d.hypotheses[0].cause_class is CauseClass.IMAGE_PULL
    assert [h.rank for h in d.hypotheses] == [1, 2]


def test_declining_to_guess_is_valid_when_it_says_what_was_missing():
    d = Diagnosis.model_validate(
        _with(hypotheses=[], insufficient_evidence=True, unknowns=["k8s_pods unavailable"])
    )

    assert d.hypotheses == []


def test_declining_without_naming_an_unknown_is_rejected():
    with pytest.raises(ValidationError, match="requires at least one unknown"):
        Diagnosis.model_validate(_with(hypotheses=[], insufficient_evidence=True, unknowns=[]))


def test_no_hypothesis_without_declining_is_rejected():
    with pytest.raises(ValidationError, match="at least one hypothesis"):
        Diagnosis.model_validate(_with(hypotheses=[]))


def test_a_hypothesis_without_supporting_evidence_is_rejected():
    h = _hypothesis(1)
    h["supporting"] = []
    with pytest.raises(ValidationError, match="supporting"):
        Diagnosis.model_validate(_with(hypotheses=[h]))


def test_more_than_three_hypotheses_is_rejected():
    """Ranks 1..4 would also break the per-rank bound, so a pass could not say which rule
    rejected them. Every rank in 1, 2, 3, 3 is in range, which leaves the length rule as
    the one that produces this message."""
    hs = [_hypothesis(1), _hypothesis(2), _hypothesis(3), _hypothesis(3)]
    with pytest.raises(ValidationError, match="at most 3 items"):
        Diagnosis.model_validate(_with(hypotheses=hs))


@pytest.mark.parametrize("ranks", [[1, 3], [2, 1], [2], [1, 1]])
def test_ranks_must_run_one_to_n_in_order(ranks):
    hs = [_hypothesis(r) for r in ranks]
    with pytest.raises(ValidationError, match="ranks must run"):
        Diagnosis.model_validate(_with(hypotheses=hs))


@pytest.mark.parametrize("cause", ["rollout_stuck", "job_backoff", "ImagePull", ""])
def test_a_cause_outside_the_vocabulary_is_rejected(cause):
    """rollout_stuck and job_backoff were removed on purpose: they name where a cause
    surfaces, not the cause. A model that still reaches for them must fail here."""
    with pytest.raises(ValidationError, match="cause_class"):
        Diagnosis.model_validate(_with(hypotheses=[_hypothesis(1, cause)]))


def test_a_citation_on_both_sides_is_rejected():
    h = _hypothesis(1)
    h["supporting"] = [FP_A, FP_B]
    h["contradicting"] = [FP_B]
    with pytest.raises(ValidationError, match="both supporting and contradicting"):
        Diagnosis.model_validate(_with(hypotheses=[h]))


@pytest.mark.parametrize("n", [0, 4])
def test_verify_carries_one_to_three_commands(n):
    h = _hypothesis(1)
    h["verify"] = [{"command": "kubectl get pods", "purpose": "state"}] * n
    with pytest.raises(ValidationError, match="verify"):
        Diagnosis.model_validate(_with(hypotheses=[h]))


def test_a_field_the_model_invents_is_rejected():
    with pytest.raises(ValidationError, match="extra"):
        Diagnosis.model_validate(_with(remediation="kubectl delete pod x"))


# The bounds were raised on 2026-10-12 because they rejected correct answers. Each is
# pinned at its edge from both sides, so a later change in either direction fails here
# rather than surfacing as a repair rate in the next eval.


def test_the_raised_bounds_are_the_ones_measured():
    assert MAX_CITATIONS == 15
    assert MAX_SUMMARY_CHARS == 900


def test_a_summary_at_the_bound_parses_and_one_past_it_is_rejected():
    Diagnosis.model_validate(_with(summary="x" * MAX_SUMMARY_CHARS))

    with pytest.raises(ValidationError, match="summary"):
        Diagnosis.model_validate(_with(summary="x" * (MAX_SUMMARY_CHARS + 1)))


@pytest.mark.parametrize("side", ["supporting", "contradicting"])
def test_citations_at_the_bound_parse_and_one_past_it_is_rejected(side):
    ids = [f"F{i}" for i in range(1, MAX_CITATIONS + 2)]
    at_bound = _hypothesis(1)
    at_bound[side] = ids[:MAX_CITATIONS]
    if side == "contradicting":
        at_bound["supporting"] = ["F99"]
    Diagnosis.model_validate(_with(hypotheses=[at_bound]))

    past = copy.deepcopy(at_bound)
    past[side] = ids
    with pytest.raises(ValidationError, match=side):
        Diagnosis.model_validate(_with(hypotheses=[past]))


def test_the_tool_schema_carries_the_same_bounds():
    """Bedrock enforces the tool schema too. A bound raised in Field() but stale in the
    schema the model is shown would be two contracts."""
    schema = tool_input_schema()
    hypothesis = schema["$defs"]["Hypothesis"]["properties"]

    assert schema["properties"]["summary"]["maxLength"] == MAX_SUMMARY_CHARS
    assert hypothesis["supporting"]["maxItems"] == MAX_CITATIONS
    assert hypothesis["contradicting"]["maxItems"] == MAX_CITATIONS


def test_the_tool_schema_offers_exactly_the_vocabulary():
    schema = tool_input_schema()
    enum = schema["$defs"]["CauseClass"]["enum"]

    assert enum == [c.value for c in CauseClass]
    assert "rollout_stuck" not in enum
    assert set(schema["required"]) == {"summary", "hypotheses", "insufficient_evidence"}


def _record(**changes) -> dict:
    base = {
        "diagnosis_id": "d1",
        "incident_id": "i1",
        "created_at": datetime(2026, 10, 4, tzinfo=UTC),
        "model": "us.anthropic.claude-sonnet-4-6",
        "prompt_version": "1",
        "bundle_sha256": "b" * 64,
        "prompt_sha256": "p" * 64,
        "outcome": "ok",
        "attempts": 1,
        "diagnosis": VALID,
    }
    base.update(changes)
    return base


def test_an_ok_record_keeps_its_diagnosis():
    r = DiagnosisRecord.model_validate(_record())

    assert r.outcome is Outcome.OK
    assert r.diagnosis is not None
    assert r.cost_usd is None


def test_an_ok_record_without_a_diagnosis_is_rejected():
    with pytest.raises(ValidationError, match="exactly when the outcome is ok"):
        DiagnosisRecord.model_validate(_record(diagnosis=None))


@pytest.mark.parametrize("outcome", ["invalid_output", "guard_rejected", "model_error"])
def test_a_failed_record_never_carries_a_diagnosis(outcome):
    """guard_rejected matters most: a diagnosis that proposed a write command must not
    survive in the journal where a reader could act on it."""
    with pytest.raises(ValidationError, match="exactly when the outcome is ok"):
        DiagnosisRecord.model_validate(_record(outcome=outcome))

    r = DiagnosisRecord.model_validate(_record(outcome=outcome, diagnosis=None, errors=["x"]))
    assert r.diagnosis is None

def test_the_diagnosis_tool_offers_no_remedies_and_old_rows_still_read_back():
    """Remedies come from their own call (prompt v9). Hypothesis keeps the field so the
    merged answer and rows from before v6 both validate."""
    hypothesis = tool_input_schema()["$defs"]["Hypothesis"]
    old_row = _with()

    assert "remediate" not in hypothesis["properties"]
    assert "remediate" not in hypothesis.get("required", [])
    assert Diagnosis.model_validate(old_row).hypotheses[0].remediate == []


def test_a_remedy_step_rejects_an_unknown_risk_and_an_invented_field():
    d = _with()
    d["hypotheses"][0]["remediate"] = [{"action": "a", "risk": "none"}]
    with pytest.raises(ValidationError, match="risk"):
        Diagnosis.model_validate(d)
    d["hypotheses"][0]["remediate"] = [{"action": "a", "risk": "low", "auto_apply": True}]
    with pytest.raises(ValidationError, match="auto_apply"):
        Diagnosis.model_validate(d)
