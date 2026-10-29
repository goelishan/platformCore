"""
The UI's view of a diagnosis, built from real model output and real evidence.

  - The crashloop case is the v3 eval's actual answer against the frozen fixture it was
    given, so citation resolution is tested on fingerprints a model really returned.
  - A live result and the same diagnosis read back from the journal must build the same
    view; otherwise the history tab shows something other than what the engineer saw.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oncall import config
from oncall import landing_zone as lz
from oncall.diagnosis import service, snapshots
from oncall.diagnosis.journal import journal
from oncall.diagnosis.schema import DiagnosisRecord
from oncall.tests.test_diagnosis_journal import open_incident
from oncall.ui import view

EVAL = Path(__file__).resolve().parents[1] / "eval"


@pytest.fixture(scope="module")
def crashloop():
    bundle = snapshots.load_file(EVAL / "fixtures" / "crashloop.json")
    path = EVAL / "reports" / "2026-10-08T1049-v3-claude-sonnet-4-6.json"
    report = json.loads(path.read_text())
    raw = next(r["record"] for r in report["runs"] if r["subject"] == "crashloop")
    return bundle, DiagnosisRecord.model_validate(raw)


def built(crashloop) -> view.DiagnosisView:
    bundle, record = crashloop
    return view.build(view.row_from_record(record), bundle, service.labels(bundle))


def test_every_citation_resolves_to_a_finding_with_the_label_the_model_used(crashloop):
    v = built(crashloop)
    cited = [e for h in v.hypotheses for e in h.supporting + h.contradicting]

    assert cited
    assert all(e.found for e in cited)
    assert all(e.label.startswith("F") for e in cited)


def test_the_leading_hypothesis_carries_the_log_line_that_names_the_cause(crashloop):
    v = built(crashloop)

    assert v.hypotheses[0].cause_class == "config_missing"
    excerpts = " ".join(e.log_excerpt for e in v.hypotheses[0].supporting)
    assert "DATABASE_URL not set" in excerpts


def test_the_first_command_is_the_leading_hypothesis_first_check(crashloop):
    v = built(crashloop)

    assert v.first_command == v.hypotheses[0].verify[0]["command"]
    assert v.first_command.startswith("kubectl ")


def test_stale_evidence_is_a_warning_and_the_model_said_so_too(crashloop):
    v = built(crashloop)

    assert any(w.startswith("evidence may be stale") for w in v.warnings)
    assert any("stale" in u.lower() for u in v.unknowns)


def test_a_rejected_diagnosis_is_a_banner_with_its_error_never_an_empty_card():
    """The shape of the real 2026-10-04 journal row: hypotheses [], no summary."""
    row = {
        "diagnosis_id": "13f6d33f", "outcome": "guard_rejected", "attempts": 1,
        "hypotheses": [], "summary": None, "unknowns": None, "cost_usd": None,
        "errors": ["attempt 1: hypothesis 2: 'kubectl get secret -n oncall-lab': "
                   "reading Secrets is not allowed"],
        "bundle_sha256": "7e6e", "prompt_sha256": "86c0", "latency_ms": 15941,
    }
    v = view.build(row, None, {})

    assert not v.ok
    assert v.banner[0] == "error" and "allowlist" in v.banner[1]
    assert v.errors == row["errors"]
    assert v.hypotheses == [] and v.first_command is None
    assert v.receipt["cost"] == "not priced"


@pytest.mark.parametrize(("runtime", "names", "never"), [
    ("local", "make -C oncall bedrock-check", "helm test"),
    ("kubernetes", "helm test oncall", "make "),
])
def test_the_model_error_hint_names_a_check_that_exists_where_the_ui_runs(
    monkeypatch, runtime, names, never
):
    """F3, drill 2026-10-13: the UI in the cluster told the engineer to run a make target,
    and the image has no Makefile."""
    monkeypatch.setattr(config, "RUNTIME", runtime)
    row = {"diagnosis_id": "d", "outcome": "model_error", "hypotheses": [], "errors": []}

    level, text = view.build(row, None, {}).banner

    assert level == "error"
    assert names in text and never not in text


def test_a_citation_the_snapshot_lacks_is_shown_as_missing_not_dropped(crashloop):
    bundle, record = crashloop
    row = view.row_from_record(record)
    row["hypotheses"][0]["supporting"].append("f" * 32)
    v = view.build(row, bundle, service.labels(bundle))

    missing = [e for e in v.hypotheses[0].supporting if not e.found]
    assert [e.label for e in missing] == ["ffffffff"]


def test_no_snapshot_says_the_evidence_cannot_be_shown(crashloop):
    _, record = crashloop
    v = view.build(view.row_from_record(record), None, {})

    assert v.warnings == ["No bundle snapshot: cited evidence cannot be shown."]
    assert all(not e.found for h in v.hypotheses for e in h.supporting)


def test_a_blind_source_is_phrased_so_absence_never_reads_as_health(crashloop):
    bundle, _ = crashloop
    sources = [s.model_copy(update={"status": "unavailable", "error": "connection refused"})
               if s.source == "k8s_logs" else s for s in bundle.sources]
    blind = view.blind_sources(bundle.model_copy(update={"sources": sources}))

    assert blind == ["k8s_logs was unavailable: connection refused"]


def test_unknowns_not_recorded_and_none_named_are_different(crashloop):
    _, record = crashloop
    failed = record.model_copy(update={"outcome": "model_error", "diagnosis": None})

    assert view.row_from_record(failed)["unknowns"] is None
    assert view.build(view.row_from_record(failed), None, {}).unknowns is None
    empty = record.model_copy(update={"diagnosis": record.diagnosis.model_copy(
        update={"unknowns": []})})
    assert view.row_from_record(empty)["unknowns"] == []


def test_a_live_result_and_its_journal_row_build_the_same_view(buffer, crashloop):
    bundle, record = crashloop
    incident = open_incident()
    record = record.model_copy(update={"incident_id": incident,
                                       "created_at": datetime(2026, 10, 12, tzinfo=UTC)})
    with lz.connect() as conn:
        journal(conn, record)

    labels = service.labels(bundle)
    live = view.build(view.row_from_record(record), bundle, labels)
    history = view.build(service.journalled(record.diagnosis_id), bundle, labels)

    assert history == live

    failed = record.model_copy(update={"diagnosis_id": "failed", "outcome": "model_error",
                                       "diagnosis": None, "errors": ["attempt 1: throttled"]})
    with lz.connect() as conn:
        journal(conn, failed)
    assert view.build(service.journalled("failed"), bundle, labels) == view.build(
        view.row_from_record(failed), bundle, labels)


@pytest.mark.parametrize(("seconds", "text"), [(30, "30 s ago"), (600, "10 min ago"),
                                                (7200, "2 h ago")])
def test_source_age_is_readable(seconds, text):
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    then = datetime.fromtimestamp(now.timestamp() - seconds, UTC).isoformat()

    assert view.age(then, now) == text
    assert view.age(None) == "never"


def test_remedies_reach_the_view_and_old_rows_show_none(crashloop):
    bundle, record = crashloop
    row = view.row_from_record(record)
    row["hypotheses"][0]["remediate"] = [
        {"action": "Restore the setting", "risk": "medium", "undo": "remove it"}]
    old = view.row_from_record(record)
    old["hypotheses"][0].pop("remediate", None)

    v = view.build(row, bundle, service.labels(bundle))

    assert v.hypotheses[0].remediate[0]["action"] == "Restore the setting"
    assert view.remedy_heading(v.hypotheses[0].remediate[0]) == "medium risk: Restore the setting"
    assert view.build(old, bundle, service.labels(bundle)).hypotheses[0].remediate == []
