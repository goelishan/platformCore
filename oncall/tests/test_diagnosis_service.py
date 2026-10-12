"""
The shared diagnose flow's read side: candidates, source health, and history across tiers.

  - History merges the buffer and the store and survives either being empty or the store
    being unreachable, because the store is most likely down during an incident.
  - A dead store is consulted once per retry interval, not once per call: each attempt
    waits out the connect timeout, and the UI reads history on every interaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oncall import landing_zone as lz
from oncall.diagnosis import service
from oncall.diagnosis.journal import journal
from oncall.diagnosis.schema import Outcome
from oncall.envelope import Severity
from oncall.tests import test_bundle as tb
from oncall.tests.test_diagnosis_journal import open_incident, record


@pytest.fixture(autouse=True)
def _no_store_unless_asked(request):
    """Tests that do not request store_db must not read whatever a shared dev store holds."""
    asked = "store_db" in request.fixturenames
    service._store_skipped_until = 0.0 if asked else float("inf")
    yield
    service._store_skipped_until = 0.0


def test_only_warnings_or_worse_are_offered_worst_first(buffer):
    now = datetime.now(UTC)
    tb.store([
        tb.make_signal(when=now - timedelta(minutes=5), key="app|Pulled|",
                       severity=Severity.INFO, payload={"reason": "Pulled"}, pod="healthy-1"),
        tb.make_signal(when=now - timedelta(minutes=6), key="app|BackOff|",
                       severity=Severity.WARNING, payload={"reason": "BackOff"}, pod="slow-1"),
        tb.make_signal(when=now - timedelta(minutes=8), key="app|ErrImagePull|",
                       severity=Severity.ERROR, payload={"reason": "ErrImagePull"}),
    ])

    found = service.subjects(tb.CLUSTER, now - timedelta(hours=1))

    assert [(s["name"], s["worst"]) for s in found] == [(tb.POD, "error"), ("slow-1", "warning")]


def test_nothing_older_than_the_window_is_offered(buffer):
    now = datetime.now(UTC)
    tb.store([tb.make_signal(when=now - timedelta(hours=8), key="app|ErrImagePull|",
                             severity=Severity.ERROR, payload={"reason": "ErrImagePull"})])

    assert service.subjects(tb.CLUSTER, now - timedelta(hours=6)) == []


def test_source_health_is_the_newest_run_per_source(buffer):
    from oncall.envelope import SignalSource, SourceStatus

    with lz.connect() as conn:
        first = lz.start_run(conn, SignalSource.K8S_PODS, tb.CLUSTER)
        lz.finish_run(conn, first, SourceStatus.UNAVAILABLE, 0, "refused")
    with lz.connect() as conn:
        second = lz.start_run(conn, SignalSource.K8S_PODS, tb.CLUSTER)
        lz.finish_run(conn, second, SourceStatus.OK, 3)

    health = service.source_health(tb.CLUSTER)

    assert [(h["source"], h["status"]) for h in health] == [("k8s_pods", "ok")]


def test_history_reads_the_buffer_when_the_store_is_down(buffer):
    incident = open_incident()
    with lz.connect() as conn:
        journal(conn, record(incident))

    rows = service.recent()

    assert [r["outcome"] for r in rows] == ["ok"]
    assert rows[0]["unknowns"] == []


def test_a_dead_store_is_tried_once_per_retry_interval(buffer, monkeypatch):
    from oncall import store

    service._store_skipped_until = 0.0
    attempts = []

    def refuse():
        attempts.append(1)
        raise ConnectionError("refused")

    monkeypatch.setattr(store, "connect", refuse)
    service.recent()
    service.recent()
    service.journalled("missing")

    assert len(attempts) == 1


def test_history_merges_tiers_without_duplicates(buffer, store_db):
    from oncall import shipper

    incident = open_incident()
    with lz.connect() as conn:
        journal(conn, record(incident, Outcome.OK, diagnosis_id="shipped"))
    shipper.ship_all()
    with lz.connect() as conn:
        conn.execute("DELETE FROM diagnoses WHERE diagnosis_id = 'shipped'")
        journal(conn, record(incident, Outcome.MODEL_ERROR, diagnosis_id="local",
                             created_at=datetime(2026, 10, 5, tzinfo=UTC)))

    rows = service.recent()

    assert [r["diagnosis_id"] for r in rows] == ["local", "shipped"]
    assert rows[1]["unknowns"] == [] and rows[0]["unknowns"] is None
    assert isinstance(rows[1]["cost_usd"], float)
