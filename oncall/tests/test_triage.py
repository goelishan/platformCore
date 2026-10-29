"""
Continuous triage: what the loop diagnoses unasked, what it leaves alone, and when it
closes an incident.

  - decide() is tested as the pure policy it is, one rule per case.
  - run_once() is tested through the real diagnose path with a fake Bedrock, so an
    automatic diagnosis is shown to be journalled, attributed and budgeted exactly as
    the loop claims, not merely that the loop called something.
  - Resolution is tested with a blind witness, because closing on silence from a dead
    collector is the failure the rule exists to prevent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from oncall import agent, config, triage
from oncall import landing_zone as lz
from oncall.envelope import Severity, SignalSource, SourceStatus, iso
from oncall.tests import test_bundle as tb
from oncall.tests import test_diagnosis_reasoner as tr

NOW = tb.NOW


def candidate(first_min=10, last_min=1, subject=tb.POD) -> triage.Candidate:
    return triage.Candidate(subject, "prod", 2, NOW - timedelta(minutes=first_min),
                            NOW - timedelta(minutes=last_min), 5)


def at(minutes_ago: float):
    return NOW - timedelta(minutes=minutes_ago)


# ---- the policy ----------------------------------------------------------------


@pytest.mark.parametrize(("c", "h", "diagnose", "reason"), [
    (candidate(first_min=1), triage.History(), False, "settling"),
    (candidate(last_min=20), triage.History(), False, "no longer seen"),
    (candidate(), triage.History(), True, "never diagnosed"),
    (candidate(), triage.History("i", at(5), "ok", at(5)), False, "nothing new"),
    (candidate(), triage.History("i", at(5), "ok", at(5), new_evidence=True), True,
     "new evidence"),
    (candidate(), triage.History("i", at(61), "ok", at(61)), True, "still failing"),
    (candidate(), triage.History("i", at(5), "model_error", None, 1), False,
     "waiting to retry"),
    (candidate(), triage.History("i", at(16), "model_error", None, 1), True,
     "retry after model_error"),
    (candidate(), triage.History("i", at(16), "model_error", None, 3), False,
     "left for a person"),
])
def test_the_policy(c, h, diagnose, reason):
    d = triage.decide(c, h, NOW)

    assert d.diagnose is diagnose
    assert reason in d.reason


# ---- reading the buffer ------------------------------------------------------------


@pytest.fixture
def failing(buffer):
    """One pod failing for ten minutes, both witnesses fresh."""
    tb.store([tb.make_signal(when=at(m), key=f"app|Error|{m}") for m in (10, 6, 1)])
    tb.record_run(SignalSource.K8S_EVENTS)
    return buffer


def test_a_subject_at_the_floor_is_a_candidate_and_one_below_it_is_not(buffer):
    tb.store([tb.make_signal(when=at(4)),
              tb.make_signal(when=at(4), pod="quiet", severity=Severity.WARNING),
              tb.make_signal(when=at(4), pod="info", severity=Severity.INFO)])

    with lz.connect() as conn:
        found = triage.candidates(conn, tb.CLUSTER, NOW)

    assert [c.subject for c in found] == [tb.POD]


def test_first_and_last_bad_come_from_the_evidence(failing):
    with lz.connect() as conn:
        (c,) = triage.candidates(conn, tb.CLUSTER, NOW)

    assert (c.first_bad, c.last_bad) == (at(10), at(1))


# ---- one pass ---------------------------------------------------------------------


def test_a_failing_subject_is_diagnosed_unasked_and_attributed_to_the_loop(failing):
    client = tr.FakeBedrock(tr.answer(tr.diagnosis()))

    report = triage.run_once(now=NOW, client=client, cluster=tb.CLUSTER)

    assert report.diagnosed == [(tb.POD, "ok")]
    with lz.connect() as conn:
        inc = conn.execute("SELECT trigger, title, state FROM incidents").fetchone()
        d = conn.execute("SELECT requested_by, outcome FROM diagnoses").fetchone()
    assert (inc["trigger"], inc["title"], inc["state"]) == ("auto", tb.POD, "diagnosed")
    assert (d["requested_by"], d["outcome"]) == ("auto", "ok")


def test_a_second_pass_with_nothing_new_costs_nothing(failing):
    client = tr.FakeBedrock(tr.answer(tr.diagnosis()))
    triage.run_once(now=NOW, client=client, cluster=tb.CLUSTER)

    report = triage.run_once(now=NOW + timedelta(seconds=120), client=client,
                             cluster=tb.CLUSTER)

    assert report.diagnosed == []
    assert report.skipped == [(tb.POD, "diagnosed; nothing new since")]
    assert len(client.requests) == 1


def test_a_new_failure_after_the_diagnosis_is_new_evidence_and_a_repeat_is_not(failing):
    triage.run_once(now=NOW, client=tr.FakeBedrock(tr.answer(tr.diagnosis())),
                    cluster=tb.CLUSTER)
    later = NOW + timedelta(minutes=3)

    def new_evidence() -> bool:
        with lz.connect() as conn:
            (c,) = triage.candidates(conn, tb.CLUSTER, later)
            return triage.history(conn, tb.CLUSTER, c).new_evidence

    tb.store([tb.make_signal(when=later - timedelta(seconds=30), key="app|Error|1")])
    repeat = new_evidence()
    tb.store([tb.make_signal(when=later - timedelta(seconds=20), key="app|OOMKilled|")])

    assert (repeat, new_evidence()) == (False, True)


def test_the_hourly_cap_leaves_the_rest_reported_not_dropped(failing, monkeypatch):
    monkeypatch.setattr(config, "TRIAGE_MAX_PER_HOUR", 1)
    tb.store([tb.make_signal(when=at(m), pod="other", key=f"o|{m}") for m in (9, 2)])
    client = tr.FakeBedrock(tr.answer(tr.diagnosis()))

    report = triage.run_once(now=NOW, client=client, cluster=tb.CLUSTER)

    assert len(report.diagnosed) == 1
    assert [r for _, r in report.skipped] == ["spend cap: never diagnosed"]


def test_calls_on_an_incident_a_person_opened_still_count_against_the_cap(failing):
    with lz.connect() as conn:
        lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual", title=tb.POD)
    triage.run_once(now=NOW, client=tr.FakeBedrock(tr.answer(tr.diagnosis())),
                    cluster=tb.CLUSTER)

    with lz.connect() as conn:
        spent = triage.auto_diagnoses_since(conn, tb.CLUSTER, NOW - timedelta(hours=1))
        trigger = conn.execute("SELECT trigger FROM incidents").fetchone()["trigger"]

    assert (spent, trigger) == (1, "manual")


def test_a_model_outage_is_retried_on_a_backoff_not_every_pass(failing):
    client = tr.FakeBedrock(tr.throttled(), tr.throttled())
    first = triage.run_once(now=NOW, client=client, cluster=tb.CLUSTER)

    again = triage.run_once(now=NOW + timedelta(minutes=2), client=client,
                            cluster=tb.CLUSTER)

    assert first.diagnosed == [(tb.POD, "model_error")]
    assert again.diagnosed == []
    assert "waiting to retry" in again.skipped[0][1]


# ---- resolution ---------------------------------------------------------------------


def _quiet_incident(minutes_old: int = 30) -> str:
    """An incident whose subject last failed long ago."""
    tb.store([tb.make_signal(when=at(minutes_old))])
    tb.record_run(SignalSource.K8S_EVENTS)
    with lz.connect() as conn:
        incident = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="auto", title=tb.POD)
        conn.execute("UPDATE incidents SET opened_at = ? WHERE incident_id = ?",
                     (iso(at(minutes_old)), incident))
    return incident


def state(incident: str) -> str:
    with lz.connect() as conn:
        return conn.execute("SELECT state FROM incidents WHERE incident_id = ?",
                            (incident,)).fetchone()["state"]


def test_an_incident_quiet_for_long_enough_closes_itself(buffer):
    incident = _quiet_incident()

    report = triage.run_once(now=NOW, cluster=tb.CLUSTER)

    assert report.resolved == [incident]
    assert state(incident) == "resolved"


def test_silence_from_a_blind_witness_never_closes_an_incident(buffer):
    """The rule's reason to exist: a dead events collector is quiet too."""
    incident = _quiet_incident()
    tb.record_run(SignalSource.K8S_EVENTS, SourceStatus.UNAVAILABLE)

    report = triage.run_once(now=NOW, cluster=tb.CLUSTER)

    assert report.blind == ["k8s_events"]
    assert report.resolved == []
    assert state(incident) != "resolved"


def test_a_subject_still_failing_keeps_its_incident_open(failing):
    with lz.connect() as conn:
        incident = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="auto", title=tb.POD)
        conn.execute("UPDATE incidents SET opened_at = ? WHERE incident_id = ?",
                     (iso(at(30)), incident))

    triage.run_once(now=NOW, client=tr.FakeBedrock(tr.answer(tr.diagnosis())),
                    cluster=tb.CLUSTER)

    assert state(incident) != "resolved"


# ---- the agent ----------------------------------------------------------------------


def test_the_agent_runs_triage_last_and_only_where_it_can_diagnose(monkeypatch):
    monkeypatch.setattr(config, "DIAGNOSE_ENABLED", True)
    monkeypatch.setattr(config, "TRIAGE_ENABLED", True)
    jobs = agent.jobs()
    assert jobs[-1].name == "triage"
    assert jobs[-1].start_after == config.TRIAGE_START_AFTER

    monkeypatch.setattr(config, "DIAGNOSE_ENABLED", False)
    assert "triage" not in [j.name for j in agent.jobs()]
