"""
The evaluation harness, offline: labels, scoring, the freeze and the run.

  - Scoring is tested on records built by hand, one property per case, because a metric
    that is subtly wrong produces a report that reads perfectly well.
  - The run is driven end to end with the fake Bedrock client: fixtures frozen from a
    real buffer, verified against their manifest, scored, reported and compared with a
    baseline.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oncall import config
from oncall.diagnosis.schema import Diagnosis, DiagnosisRecord, Outcome
from oncall.envelope import Severity
from oncall.eval import cases
from oncall.eval.metrics import CITATION_ERROR, Case, Rate, Run, regression, summarise
from oncall.tests import test_bundle as tb
from oncall.tests import test_diagnosis_reasoner as tr

EVAL = Path(__file__).resolve().parents[1] / "eval"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"eval_{name}", EVAL / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


freeze = _load("freeze")
run_eval = _load("run")


def record(*causes: str, outcome=Outcome.OK, attempts=1, latency=1000, tokens=(100, 10),
           cost=0.01, errors=(), insufficient=False) -> DiagnosisRecord:
    diagnosis = None
    if outcome == Outcome.OK:
        diagnosis = Diagnosis.model_validate({
            "summary": "s",
            "insufficient_evidence": insufficient,
            "unknowns": ["u"] if insufficient else [],
            "hypotheses": [] if insufficient else [{
                "rank": i, "cause_class": c, "title": "t", "mechanism": "m",
                "confidence": "low", "supporting": ["a" * 32], "contradicting": [],
                "verify": [{"command": "kubectl get pods", "purpose": "p"}],
            } for i, c in enumerate(causes, start=1)],
        })
    return DiagnosisRecord(
        diagnosis_id="d", incident_id="i", created_at=datetime(2026, 10, 4, tzinfo=UTC),
        model="m", prompt_version="1", bundle_sha256="b", prompt_sha256="p",
        outcome=outcome, attempts=attempts, input_tokens=tokens[0], output_tokens=tokens[1],
        latency_ms=latency, cost_usd=cost, diagnosis=diagnosis, errors=list(errors),
    )


CASE = Case("crashloop", "config_missing", ("app_crash",))


# ---- labels ----------------------------------------------------------------


def test_the_shipped_golden_labels_load_and_use_the_vocabulary():
    golden = cases.load(EVAL / "golden.yaml")

    assert {c.subject for c in golden if not c.scored} == {"stuckterminating", "cluster"}
    assert cases.subject_of("cluster") is None
    assert next(c for c in golden if c.subject == "stuckrollout").expected == "image_pull"


def test_a_label_outside_the_vocabulary_is_refused(tmp_path):
    (tmp_path / "golden.yaml").write_text("x:\n  expected: rollout_stuck\n")

    with pytest.raises(ValueError, match="outside CauseClass"):
        cases.load(tmp_path / "golden.yaml")


# ---- scoring ---------------------------------------------------------------


def test_also_accept_counts_for_top3_and_never_for_top1():
    run = Run(CASE, record("app_crash"))

    assert run.top1 is False
    assert run.top3 is True


def test_the_label_anywhere_in_three_is_top3_and_only_rank_one_is_top1():
    run = Run(CASE, record("image_pull", "other", "config_missing"))

    assert (run.top1, run.top3) == (False, True)


def test_a_run_without_a_diagnosis_is_wrong_not_left_out():
    s = summarise([Run(CASE, record("config_missing")),
                   Run(CASE, record(outcome=Outcome.MODEL_ERROR))])

    assert (s.top1.hits, s.top1.total) == (1, 2)


def test_unscored_cases_count_for_safety_and_cost_but_not_accuracy():
    unscored = Case("stuckterminating", "termination_stuck", scored=False)
    s = summarise([Run(CASE, record("config_missing")),
                   Run(unscored, record(outcome=Outcome.GUARD_REJECTED))])

    assert s.top1.total == 1
    assert (s.command_safety.hits, s.command_safety.total) == (1, 2)


def test_citation_validity_reads_the_errors_of_every_attempt():
    s = summarise([
        Run(CASE, record("config_missing", attempts=2,
                         errors=[f"attempt 1: F9 {CITATION_ERROR}"])),
        Run(CASE, record("config_missing")),
    ])

    assert (s.citation_validity.hits, s.first_attempt_valid.hits) == (1, 1)


def test_insufficient_evidence_is_a_rate_over_answered_runs():
    s = summarise([Run(CASE, record(insufficient=True)),
                   Run(CASE, record("config_missing")),
                   Run(CASE, record(outcome=Outcome.MODEL_ERROR))])

    assert (s.insufficient_evidence.hits, s.insufficient_evidence.total) == (1, 2)


def test_p95_is_a_latency_that_was_observed():
    """Nearest rank, rounding up: with nineteen values p95 is the nineteenth and p50 the
    tenth. Twenty values would not do here, because 95% of 20 is exactly 19 and a rank
    rounded down would land on the same value."""
    runs = [Run(CASE, record("config_missing", latency=ms)) for ms in range(100, 2000, 100)]
    s = summarise(runs)

    assert (s.latency_p50_ms, s.latency_p95_ms) == (1000, 1900)


def test_cost_is_absent_when_any_run_had_no_price():
    s = summarise([Run(CASE, record("config_missing")),
                   Run(CASE, record("config_missing", cost=None))])

    assert s.mean_cost_usd is None and s.total_cost_usd is None


def test_agreement_measures_each_subject_against_its_own_modal_answer():
    """A subject run once cannot disagree with itself, so it is left out rather than
    counted as perfect agreement."""
    other = Case("oomkill", "oom_killed")
    once = Case("badimage", "image_pull")
    s = summarise([Run(CASE, record("config_missing")), Run(CASE, record("config_missing")),
                   Run(CASE, record("app_crash")), Run(other, record("oom_killed")),
                   Run(other, record("oom_killed")), Run(once, record("image_pull"))])

    assert (s.agreement.hits, s.agreement.total) == (4, 5)


@pytest.mark.parametrize(("hits", "baseline", "regressed"), [(10, 11, False), (9, 11, True)])
def test_one_case_below_baseline_is_noise_and_two_is_a_regression(hits, baseline, regressed):
    assert regression(Rate(hits, 14), Rate(baseline, 14)) is regressed


@pytest.mark.parametrize(("run", "baseline", "regressed"), [
    # A repeat-3 baseline does not condemn a plain run that is just as accurate...
    (Rate(15, 15), Rate(44, 45), False),
    (Rate(14, 15), Rate(44, 45), False),
    # ...and a plain baseline does not wave through a repeat-3 run that lost a third.
    (Rate(30, 45), Rate(15, 15), True),
    (Rate(44, 45), Rate(15, 15), False),
    (Rate(43, 45), Rate(15, 15), True),
])
def test_runs_and_baselines_of_different_sizes_compare_as_rates(run, baseline, regressed):
    assert regression(run, baseline) is regressed


def test_an_empty_run_or_baseline_is_not_a_regression():
    assert regression(Rate(0, 0), Rate(15, 15)) is False
    assert regression(Rate(10, 15), Rate(0, 0)) is False


# ---- freeze and run --------------------------------------------------------


@pytest.fixture
def root(buffer, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", tr.MODEL)
    tb.store([tb.make_signal(when=tb.NOW - timedelta(minutes=5), key="app|ErrImagePull|",
                             severity=Severity.ERROR, payload={"reason": "ErrImagePull"})])
    out = tmp_path / "eval"
    out.mkdir()
    (out / "golden.yaml").write_text(
        f"{tb.POD}:\n  expected: image_pull\ncluster:\n  scored: false\n"
    )
    assert freeze.main(["--cluster", tb.CLUSTER, "--root", str(out)]) == 0
    return out


def test_freeze_writes_each_fixture_with_its_receipt(root):
    manifest = json.loads((root / "fixtures" / "manifest.json").read_text())

    assert set(manifest) == {tb.POD, "cluster"}
    assert (root / "fixtures" / f"{tb.POD}.json").exists()


def test_freeze_refuses_a_fixture_a_redaction_rule_still_fires_on(root, monkeypatch, capsys):
    real = freeze.assembler.assemble

    def leaky(*a, **k):
        b = real(*a, **k)
        if b.findings:
            b.findings[0].facts["image"] = "registry/app password=hunter2trombone"
        return b

    monkeypatch.setattr(freeze.assembler, "assemble", leaky)

    assert freeze.main(["--cluster", tb.CLUSTER, "--root", str(root)]) == 1
    assert "hunter2trombone" not in (root / "fixtures" / f"{tb.POD}.json").read_text()
    assert "REFUSED" in capsys.readouterr().out


def test_text_redaction_already_cleaned_is_not_refused():
    """The live leakylogger excerpt of 2026-10-04: already redacted, and refused when the
    check read the JSON text, where the newline after <redacted> is the two characters
    \\n and the placeholder no longer ends a line."""
    excerpt = (
        "using <aws-access-key> for uploads\n"
        "2026-10-04T13:55:58.683621261Z Authorization: <redacted>\n"
        "2026-10-04T13:55:58.683622344Z <private-key>\n"
    )
    dump = {"findings": [{"facts": {"excerpt": excerpt}}]}

    assert freeze.rules_firing(dump) == []


def test_freeze_writes_a_fixture_whose_redaction_was_already_done(root, monkeypatch):
    """End to end, so the freeze itself is held to the per-string check and not only the
    helper: the excerpt below was refused on the live lab by a scan of the JSON text."""
    real = freeze.assembler.assemble

    def redacted(*a, **k):
        b = real(*a, **k)
        if b.findings:
            b.findings[0].facts["excerpt"] = (
                "2026-10-04T13:55:58Z Authorization: <redacted>\n2026-10-04T13:55:59Z next"
            )
        return b

    monkeypatch.setattr(freeze.assembler, "assemble", redacted)

    assert freeze.main(["--cluster", tb.CLUSTER, "--root", str(root)]) == 0
    assert "Authorization: <redacted>" in (root / "fixtures" / f"{tb.POD}.json").read_text()


def test_a_secret_in_any_nested_string_is_found():
    dump = {"findings": [{"trends": {"message": ["ok", "password=hunter2trombone"]}}]}

    assert freeze.rules_firing(dump) == ["assignment"]


def test_a_run_scores_reports_and_sets_a_baseline(root):
    client = tr.FakeBedrock(*[tr.answer(tr.diagnosis())] * 2)

    assert run_eval.main(["--root", str(root), "--accept"], client=client) == 0

    [md] = (root / "reports").glob("*.md")
    report = md.read_text()
    assert "| Top-1 accuracy | 1/1 (100%) |" in report
    assert f"| {tb.POD} | image_pull | image_pull | yes |" in report
    assert json.loads((root / "baseline.json").read_text())["top1_hits"] == 1


def _wrong() -> dict:
    d = tr.diagnosis()
    d["hypotheses"][0]["cause_class"] = "oom_killed"
    return d


def test_check_fails_a_run_that_falls_below_the_baseline(root):
    (root / "baseline.json").write_text(json.dumps({"top1_hits": 1, "scored": 1}))
    client = tr.FakeBedrock(*[tr.answer(_wrong())] * 6)

    assert run_eval.main(["--root", str(root), "--check", "--repeat", "3"], client=client) == 1


def test_check_passes_a_plain_run_against_a_repeat_three_baseline(root):
    """The case that used to fail: 1/1 against an accepted 3/3 is the same accuracy."""
    (root / "baseline.json").write_text(json.dumps({"top1_hits": 3, "scored": 3}))
    client = tr.FakeBedrock(*[tr.answer(tr.diagnosis())] * 2)

    assert run_eval.main(["--root", str(root), "--check"], client=client) == 0


def test_a_fixture_edited_after_freezing_is_refused(root):
    path = root / "fixtures" / f"{tb.POD}.json"
    path.write_text(path.read_text().replace("ErrImagePull", "OOMKilled"))

    with pytest.raises(SystemExit, match="no longer matches manifest"):
        run_eval.main(["--root", str(root)], client=tr.FakeBedrock())


def test_freeze_refuses_stale_evidence_unless_told_otherwise(root, monkeypatch, capsys):
    """Every source here last ran before the freeze, so a zero threshold makes all of
    them stale. A fixture is replayed for months; one frozen over a lab that had moved
    on teaches the eval a cluster that no longer existed."""
    monkeypatch.setattr(config, "BUNDLE_STALE_SECONDS", 0)
    before = (root / "fixtures" / f"{tb.POD}.json").read_text()

    assert freeze.main(["--cluster", tb.CLUSTER, "--root", str(root)]) == 1
    assert "collect again" in capsys.readouterr().out
    assert (root / "fixtures" / f"{tb.POD}.json").read_text() == before

    assert freeze.main(["--cluster", tb.CLUSTER, "--root", str(root), "--allow-stale"]) == 0
