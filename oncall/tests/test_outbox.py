"""
Outbox contract tests.

The outbox is the seam between two databases, and seams are where data goes missing.
Everything pinned here is a property that, if it broke, would lose evidence silently:
enqueue happening inside the write's own transaction, ordering that makes a prefix safe
to clear, and clearing that happens strictly after the store has acknowledged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oncall import landing_zone as lz
from oncall.envelope import Owner, Signal, SignalKind, SignalSource, Subject
from oncall.landing_zone import outbox

CLUSTER = "test-cluster"
T0 = datetime(2026, 8, 12, 3, 14, 0, tzinfo=UTC)


def make_signal(name: str = "api-7f9", key: str = "app|Error|1") -> Signal:
    return Signal(
        source=SignalSource.K8S_PODS,
        kind=SignalKind.POD_STATE,
        cluster=CLUSTER,
        namespace="prod",
        event_time=T0,
        subject=Subject(kind="Pod", name=name),
        owner=Owner(kind="Deployment", name="api"),
        dedupe_key=key,
        payload={"exit_code": 1},
    )


# ---- enqueue is part of the write ------------------------------------------


def test_writing_signals_queues_them(buffer):
    sig = make_signal()

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [sig])

    with lz.connect() as conn:
        _, batch = lz.pending(conn, 100)

    assert batch[outbox.SIGNAL] == [sig.signal_id]


def test_a_rolled_back_write_queues_nothing(buffer):
    """The property the whole design rests on. If the enqueue could commit separately
    from the write, the shipper would eventually look up a row that never existed — or
    worse, miss one that did."""
    with pytest.raises(RuntimeError), lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [make_signal()])
        raise RuntimeError("collector blew up after writing")

    with lz.connect() as conn:
        assert lz.depth(conn) == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] == 0


def test_incidents_and_diagnoses_are_queued_too(buffer):
    """They are created during an incident, which is when the store is most likely to be
    unreachable. Writing them straight through would mean the one moment findings cannot
    be recorded is the moment there are findings."""
    with lz.connect() as conn:
        incident = lz.open_incident(conn, CLUSTER, "prod", trigger="manual")
        diagnosis = lz.record_diagnosis(
            conn, incident, model="test", bundle_sha256="abc", hypotheses=[], commands=[]
        )

    with lz.connect() as conn:
        _, batch = lz.pending(conn, 100)

    assert batch[outbox.INCIDENT] == [incident]
    assert batch[outbox.DIAGNOSIS] == [diagnosis]


def test_attaching_an_incident_requeues_the_signals(buffer):
    """incident_id is back-filled by an UPDATE, long after the rows first shipped. It is
    also the field that exempts a signal from retention as eval corpus, so a store that
    never learns about it will delete evidence it was supposed to keep."""
    sig = make_signal()

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [sig])
        watermark, _ = lz.pending(conn, 100)
        lz.clear_through(conn, watermark)

    with lz.connect() as conn:
        incident = lz.open_incident(conn, CLUSTER, "prod", trigger="manual")
        attached = lz.attach_signals(
            conn, incident, CLUSTER, T0 - _hour(), T0 + _hour()
        )

    with lz.connect() as conn:
        _, batch = lz.pending(conn, 100)

    assert attached == 1
    assert batch[outbox.SIGNAL] == [sig.signal_id]


def _hour():
    from datetime import timedelta

    return timedelta(hours=1)


# ---- reading a batch -------------------------------------------------------


def test_repeated_writes_cross_the_wire_once(buffer):
    """A signal upserted eight times between shipping cycles is eight outbox entries and
    one row to send. Collapsing on read rather than suppressing on write keeps the hot
    path free of a lookup, and the shipper sends current state anyway."""
    sig = make_signal()

    for _ in range(8):
        with lz.connect() as conn:
            run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
            lz.write_signals(conn, run, [sig])

    with lz.connect() as conn:
        assert lz.depth(conn) == 8
        _, batch = lz.pending(conn, 100)

    assert batch[outbox.SIGNAL] == [sig.signal_id]


def test_a_batch_is_a_prefix_of_the_queue(buffer):
    """Rows come back in seq order, so the watermark covers everything older than it.
    Clearing a prefix can never strand an older entry behind a newer one."""
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(
            conn, run, [make_signal(name=f"pod-{i}", key=f"app|Error|{i}") for i in range(5)]
        )

    with lz.connect() as conn:
        watermark, batch = lz.pending(conn, 3)

    assert watermark == 3
    assert len(batch[outbox.SIGNAL]) == 3


def test_an_empty_queue_reports_no_watermark(buffer):
    with lz.connect() as conn:
        assert lz.pending(conn, 100) == (0, {})


# ---- clearing --------------------------------------------------------------


def test_clearing_removes_only_up_to_the_watermark(buffer):
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(
            conn, run, [make_signal(name=f"pod-{i}", key=f"app|Error|{i}") for i in range(5)]
        )

    with lz.connect() as conn:
        watermark, _ = lz.pending(conn, 3)
        lz.clear_through(conn, watermark)
        assert lz.depth(conn) == 2


def test_seq_never_goes_backwards_after_a_drain(buffer):
    """AUTOINCREMENT, not a bare INTEGER PRIMARY KEY. Without it SQLite reuses the
    rowids of deleted rows, and this table is emptied continuously — a reused seq would
    let a later batch carry a watermark lower than one already cleared."""
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [make_signal(name="first")])
        first, _ = lz.pending(conn, 100)
        lz.clear_through(conn, first)

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [make_signal(name="second", key="app|Error|2")])
        second, _ = lz.pending(conn, 100)

    assert second > first


def test_unshipped_ids_are_reported_for_the_sweep(buffer):
    """The one guard between routine cleanup and silent loss during a store outage."""
    sig = make_signal()

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [sig])
        assert lz.unshipped_signal_ids(conn) == {sig.signal_id}

        watermark, _ = lz.pending(conn, 100)
        lz.clear_through(conn, watermark)
        assert lz.unshipped_signal_ids(conn) == set()
