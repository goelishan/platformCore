"""
Every real diagnosis evaluated and stored: checks, outcome, the engineer's verdict, and
the paths that turn those into improvements (review, promotion into the eval).

  - Diagnoses are made through the real service path with a fake Bedrock, so what is
    evaluated is a journalled row and a saved snapshot, exactly as in production.
  - Checks are tested one flag at a time against a diagnosis that passes everything, so
    each case shows the rule firing and nothing else changing.
  - The store cases read Postgres, not the buffer: an evaluation that never ships is
    lost the day the buffer is rebuilt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from oncall import agent, shipper
from oncall import landing_zone as lz
from oncall.diagnosis import service, snapshots
from oncall.envelope import Severity, SignalSource, iso
from oncall.eval import cases, freeze, online, promote, review
from oncall.tests import test_bundle as tb
from oncall.tests import test_diagnosis_reasoner as tr

NOW = tb.NOW


def good(**changes) -> dict:
    """A diagnosis no check objects to on the lab bundle: it cites the error, admits the
    sources that never ran, is not high confidence while they are blind, offers a fix."""
    d = tr.diagnosis()
    d["unknowns"] = ["k8s_logs never ran, so the container's own output was not seen"]
    d["hypotheses"][0]["confidence"] = "medium"
    for k, v in changes.items():
        if k in d:
            d[k] = v
        else:
            d["hypotheses"][0][k] = v
    return d


@pytest.fixture
def lab(buffer):
    tb.store([
        tb.make_signal(when=NOW - timedelta(minutes=8), key="app|ErrImagePull|",
                       severity=Severity.ERROR, payload={"reason": "ErrImagePull"}),
        tb.make_signal(when=NOW - timedelta(minutes=2), key="app|Pulled|",
                       severity=Severity.INFO, payload={"reason": "Pulled"}),
    ])
    tb.record_run(SignalSource.K8S_EVENTS)
    return buffer


def diagnose(*replies, trigger="manual", remedies=()):
    b = service.assemble(tb.CLUSTER, tb.POD)
    return service.diagnose(b, tb.CLUSTER, tb.POD,
                            client=tr.FakeBedrock(*replies, remedies=remedies),
                            trigger=trigger)


def checks_of(diagnosis_id: str) -> dict:
    return online.evaluations_for(diagnosis_id)["checks"]


# ---- checks ---------------------------------------------------------------------------


def test_a_diagnosis_is_checked_the_moment_it_is_journalled(lab):
    d = diagnose(tr.answer(good()))

    c = checks_of(d.record.diagnosis_id)

    assert (c["verdict"], c["score"]) == ("pass", 1.0)
    assert c["details"]["flags"] == []
    assert c["details"]["requested_by"] == "manual"


def test_a_failed_call_is_checked_as_a_fail_with_score_zero(lab):
    d = diagnose(tr.throttled())

    c = checks_of(d.record.diagnosis_id)

    assert (c["verdict"], c["score"], c["details"]["flags"]) == ("fail", 0.0, ["no_diagnosis"])


RISKY_FIRST = [
    {"action": "drain", "command": "kubectl drain node-1", "risk": "high", "undo": "u"},
    {"action": "restart", "command": "kubectl rollout restart deployment/app",
     "risk": "medium", "undo": "u"}]


@pytest.mark.parametrize(("changes", "steps", "flag"), [
    ({"supporting": ["F2"]}, None, "worst_evidence_uncited"),
    ({"unknowns": []}, None, "gaps_unacknowledged"),
    ({"confidence": "high"}, None, "high_confidence_while_blind"),
    ({}, RISKY_FIRST, "riskiest_step_first"),
])
def test_each_check_raises_its_flag_and_only_that_one(lab, changes, steps, flag):
    remedies = [tr.remedy_answer(steps)] if steps else ()
    d = diagnose(tr.answer(good(**changes)), remedies=remedies)

    c = checks_of(d.record.diagnosis_id)

    assert c["verdict"] == "warn"
    assert c["details"]["flags"] == [flag]
    assert 0 < c["score"] < 1


def test_a_repaired_answer_is_flagged(lab):
    d = diagnose(tr.answer(good(supporting=["F9"])), tr.answer(good()))

    assert checks_of(d.record.diagnosis_id)["details"]["flags"] == ["needed_repair"]


def test_checks_that_need_the_snapshot_do_not_apply_without_it_and_do_not_count():
    row = {"diagnosis_id": "d", "outcome": "ok", "attempts": 1,
           "hypotheses": good(remediate=[tr.REMEDY])["hypotheses"], "unknowns": []}

    e = online.checks(row, None)

    assert e.details["checks"]["worst_cited"] is None
    assert e.details["checks"]["gaps_acknowledged"] is None
    assert (e.verdict, e.score) == ("pass", 1.0)


# ---- the evaluate job -------------------------------------------------------------------


def test_the_job_checks_whatever_was_missed(lab, monkeypatch):
    monkeypatch.setattr(service, "_evaluate", lambda _id: None)
    d = diagnose(tr.answer(good()))
    assert online.evaluations_for(d.record.diagnosis_id) == {}

    done = online.evaluate_pending()

    assert done["checks"] == 1
    assert "checks" in online.evaluations_for(d.record.diagnosis_id)


def test_an_outcome_waits_for_the_incident_or_the_horizon(lab):
    d = diagnose(tr.answer(good()))

    assert online.evaluate_pending(now=datetime.now(UTC))["outcome"] == 0
    later = datetime.now(UTC) + online.OUTCOME_HORIZON + timedelta(minutes=1)
    assert online.evaluate_pending(now=later)["outcome"] == 1
    o = online.evaluations_for(d.record.diagnosis_id)["outcome"]
    assert o["verdict"] == "persisted"


def test_outcome_itself_declines_an_open_incident_inside_the_horizon():
    """The job's query filters on the horizon too; the function must not rely on it."""
    row = {"created_at": iso(NOW), "hypotheses": good()["hypotheses"]}

    assert online.outcome(row, {"state": "diagnosed"}, [], NOW + timedelta(minutes=30)) is None
    assert online.outcome(row, {"state": "diagnosed"}, [], NOW + timedelta(hours=3)) is not None


def test_a_resolved_incident_records_time_to_resolve_and_whether_later_diagnoses_agreed(lab):
    first = diagnose(tr.answer(good()))
    other = good()
    other["hypotheses"][0]["cause_class"] = "app_crash"
    diagnose(tr.answer(other))
    with lz.connect() as conn:
        lz.close_incident(conn, first.incident_id)

    online.evaluate_pending()

    o = online.evaluations_for(first.record.diagnosis_id)["outcome"]
    assert o["verdict"] == "resolved"
    assert o["details"]["later_causes"] == ["app_crash"]
    assert o["details"]["cause_stable"] is False
    assert o["details"]["resolved_after_minutes"] >= 0


def test_persisted_at_the_horizon_becomes_resolved_once_the_incident_closes(lab):
    d = diagnose(tr.answer(good()))
    later = datetime.now(UTC) + online.OUTCOME_HORIZON + timedelta(minutes=1)
    online.evaluate_pending(now=later)
    with lz.connect() as conn:
        lz.close_incident(conn, d.incident_id)

    online.evaluate_pending(now=later)

    assert online.evaluations_for(d.record.diagnosis_id)["outcome"]["verdict"] == "resolved"


def test_the_agent_runs_the_evaluate_job():
    assert "evaluate" in [j.name for j in agent.jobs()]


# ---- the engineer's verdict -----------------------------------------------------------------


def test_a_correct_verdict_takes_the_models_cause_and_a_wrong_one_needs_another(lab):
    d = diagnose(tr.answer(good()))
    did = d.record.diagnosis_id

    right = online.record_human(did, "correct", remedy="worked")
    assert (right.cause_class, right.details["top1_match"], right.score) == ("image_pull", True, 1)

    with pytest.raises(ValueError, match="model's own"):
        online.record_human(did, "wrong", "image_pull")
    wrong = online.record_human(did, "wrong", "config_missing", notes="secret renamed")

    assert (wrong.details["top1_match"], wrong.score) == (False, 0.0)
    h = online.evaluations_for(did)["human"]
    assert (h["verdict"], h["cause_class"], h["notes"]) == ("wrong", "config_missing",
                                                             "secret renamed")


@pytest.mark.parametrize(("verdict", "cause", "remedy", "match"), [
    ("right", None, None, "verdict"),
    ("wrong", "dns", None, "CauseClass"),
    ("partial", None, "maybe", "remedy"),
])
def test_a_verdict_outside_the_vocabulary_is_refused(lab, verdict, cause, remedy, match):
    d = diagnose(tr.answer(good()))

    with pytest.raises(ValueError, match=match):
        online.record_human(d.record.diagnosis_id, verdict, cause, remedy)


# ---- the store --------------------------------------------------------------------------


def test_evaluations_and_the_snapshot_reach_the_store(lab, store_db):
    d = diagnose(tr.answer(good()))
    online.record_human(d.record.diagnosis_id, "correct")

    shipper.ship_all()

    with store_db.connect() as conn:
        rows = store_db.fetch_all(conn, "SELECT evaluator, verdict, details FROM evaluations "
                                        "ORDER BY evaluator")
        bundle = store_db.snapshot(conn, d.record.bundle_sha256)
    assert [(r["evaluator"], r["verdict"]) for r in rows] == [("checks", "pass"),
                                                               ("human", "correct")]
    assert isinstance(rows[0]["details"], dict)
    assert bundle is not None and bundle["subject"] == tb.POD


# ---- promotion into the eval ------------------------------------------------------------------


@pytest.fixture
def eval_root(tmp_path):
    root = tmp_path / "eval"
    (root / "fixtures").mkdir(parents=True)
    (root / "golden.yaml").write_text("crashloop:\n  expected: config_missing\n")
    (root / "fixtures" / "manifest.json").write_text('{"crashloop": "abc"}\n')
    return root


def test_a_labelled_diagnosis_becomes_a_production_case_the_eval_can_load(lab, eval_root):
    d = diagnose(tr.answer(good()))
    online.record_human(d.record.diagnosis_id, "wrong", "config_missing", notes="it's the key")

    path = promote.promote(d.record.diagnosis_id, eval_root, terms=["configmap"])

    golden = {c.subject: c for c in cases.load(eval_root / "golden.yaml")}
    case = golden[path.stem]
    assert (case.expected, case.source, case.remedy_terms) == ("config_missing", "production",
                                                               ("configmap",))
    assert "it's the key" in case.rationale
    manifest = json.loads((eval_root / "fixtures" / "manifest.json").read_text())
    b = snapshots.load_file(path)
    assert manifest[path.stem] == d.record.bundle_sha256
    assert manifest["crashloop"] == "abc"
    assert b.subject == tb.POD


def test_promotion_needs_a_verdict_with_a_cause_and_a_free_name(lab, eval_root):
    d = diagnose(tr.answer(good()))
    with pytest.raises(ValueError, match="no engineer verdict"):
        promote.promote(d.record.diagnosis_id, eval_root)

    online.record_human(d.record.diagnosis_id, "correct")
    with pytest.raises(ValueError, match="already a case"):
        promote.promote(d.record.diagnosis_id, eval_root, name="crashloop")


def test_promotion_falls_back_to_the_store_when_the_snapshot_is_not_on_this_machine(
        lab, store_db, eval_root):
    d = diagnose(tr.answer(good()))
    online.record_human(d.record.diagnosis_id, "correct")
    shipper.ship_all()
    snapshots.path_for(d.record.bundle_sha256).unlink()

    path = promote.promote(d.record.diagnosis_id, eval_root)

    assert snapshots.load_file(path).subject == tb.POD


def test_freeze_keeps_a_production_case_and_its_manifest_line(lab, eval_root):
    (eval_root / "golden.yaml").write_text(
        "prod-x:\n  expected: image_pull\n  source: production\n")
    (eval_root / "fixtures" / "manifest.json").write_text('{"prod-x": "receipt"}\n')

    freeze.main(["--cluster", tb.CLUSTER, "--root", str(eval_root)])

    assert json.loads((eval_root / "fixtures" / "manifest.json").read_text()) == {
        "prod-x": "receipt"}


# ---- the review --------------------------------------------------------------------------


def test_the_review_states_accuracy_over_labels_and_names_the_promote_command(lab, store_db):
    good_one = diagnose(tr.answer(good()))
    bad_one = diagnose(tr.answer(good(unknowns=[])))
    online.record_human(good_one.record.diagnosis_id, "correct")
    online.record_human(bad_one.record.diagnosis_id, "wrong", "config_missing")
    diagnose(tr.answer(good()))

    shipper.ship_all()
    late = diagnose(tr.answer(good()))
    online.record_human(late.record.diagnosis_id, "partial", "app_crash")

    tier, rows = review.load(datetime.now(UTC) - timedelta(days=1))
    text = review.render(tier, NOW - timedelta(days=1), rows, NOW)
    s = review.summarise(rows)

    # Shipped and unshipped together: the last diagnosis and its verdict are buffer-only.
    assert tier == "store and buffer"
    assert (s["human_top1"].hits, s["human_top1"].total) == (1, 3)
    assert f"eval-promote DIAGNOSIS={bad_one.record.diagnosis_id}" in text
    assert f"eval-promote DIAGNOSIS={late.record.diagnosis_id}" in text
    assert "1/4 diagnoses have no engineer verdict" in text
    assert "gaps_unacknowledged on 1/4" in text


def test_a_flag_below_the_threshold_is_not_advice():
    rows = [{"diagnosis_id": str(i), "outcome": "ok", "attempts": 1, "hypotheses": [],
             "errors": [], "evaluations": {"checks": {
                 "verdict": "warn" if i == 0 else "pass", "score": 0.8,
                 "details": {"flags": ["gaps_unacknowledged"] if i == 0 else []}}}}
            for i in range(10)]

    assert not any("gaps_unacknowledged" in a for a in review.advice(rows, review.summarise(rows)))


def test_evaluations_are_exported_with_every_verdict_seeded(lab, store_db, monkeypatch):
    from oncall.tests.test_exporter import scrape, value

    monkeypatch.setattr("oncall.exporter.journal.config.BEDROCK_MODEL_ID", tr.MODEL)
    d = diagnose(tr.answer(good(unknowns=[])))
    online.record_human(d.record.diagnosis_id, "wrong", "config_missing")
    shipper.ship_all()

    samples = scrape()
    labels = {"model": tr.MODEL, "prompt_version": d.record.prompt_version}

    assert value(samples, "oncall_evaluations", evaluator="checks", verdict="warn", **labels) == 1
    assert value(samples, "oncall_evaluations", evaluator="human", verdict="correct", **labels) == 0
    assert value(samples, "oncall_evaluation_flags", flag="gaps_unacknowledged", **labels) == 1
    assert value(samples, "oncall_human_labelled", **labels) == 1
    assert value(samples, "oncall_human_top1_matches", **labels) == 0
    assert value(samples, "oncall_evaluation_backlog") == 0


def test_an_incident_opened_long_ago_is_used_as_written(lab):
    """iso() round-trips through the job's queries: a guard against a format drift that
    would make every outcome look due, or none."""
    d = diagnose(tr.answer(good()))
    with lz.connect() as conn:
        conn.execute("UPDATE diagnoses SET created_at = ? WHERE diagnosis_id = ?",
                     (iso(datetime.now(UTC) - timedelta(hours=3)), d.record.diagnosis_id))

    assert online.evaluate_pending()["outcome"] == 1
