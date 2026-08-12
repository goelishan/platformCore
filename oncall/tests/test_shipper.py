"""
Shipper tests: the seam between the two tiers.

The claim being tested is narrow and important. A store outage must cost the agent its
history, never its evidence — collection carries on, the buffer absorbs, the queue holds,
and everything back-fills on recovery. And because delivery is at-least-once, a re-sent
batch has to be indistinguishable from one sent exactly once.

Needs a real Postgres; skips without one.

    docker compose -f oncall/dev/compose.yaml up -d
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# Before importing shipper, which reaches the store package and pulls in psycopg at
# module level. A missing driver would otherwise be a collection error rather than a
# skip, and one collection error fails the whole run.
pytest.importorskip("psycopg_pool", reason="psycopg not installed; pip install -r oncall/requirements.txt")

from oncall import config, shipper  # noqa: E402
from oncall import landing_zone as lz
from oncall.envelope import (
    Owner,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
)
from oncall.store import connection as store_connection

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


def collect(signals: list[Signal]) -> None:
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, signals)
        lz.finish_run(conn, run, SourceStatus.OK, len(signals))


def stored_signals(store) -> int:
    with store.connect() as conn:
        return store.fetch_one(conn, "SELECT COUNT(*) AS n FROM signals")["n"]


# ---- the happy path --------------------------------------------------------


def test_an_empty_queue_is_not_an_error(buffer, store_db):
    """EMPTY, not OK-with-zero. "Nothing to send" and "sent nothing" read the same in a
    count and differently in a status, which is the same distinction three-state source
    status makes one tier down."""
    assert shipper.ship_once() == (SourceStatus.EMPTY, 0)


def test_shipping_moves_rows_and_clears_the_queue(buffer, store_db):
    collect([make_signal()])

    status, written = shipper.ship_once()

    assert (status, written) == (SourceStatus.OK, 1)
    assert stored_signals(store_db) == 1
    assert shipper.backlog() == 0


def test_the_buffer_keeps_its_rows_after_shipping(buffer, store_db):
    """The buffer is not a queue that empties on delivery. Recent evidence is what the
    assembler reads on the critical path, and it has to be there whether or not the
    store happens to be reachable."""
    collect([make_signal()])
    shipper.ship_once()

    with lz.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] == 1


def test_an_incident_and_its_evidence_arrive_together(buffer, store_db):
    """Ordering matters because signals carry a foreign key to incidents. The outbox is
    read as a prefix in seq order and an incident is always enqueued before anything
    referencing it, so the parent is never missing."""
    collect([make_signal()])

    with lz.connect() as conn:
        incident = lz.open_incident(conn, CLUSTER, "prod", trigger="manual")
        lz.attach_signals(conn, incident, CLUSTER, T0 - timedelta(hours=1), T0 + timedelta(hours=1))

    shipper.ship_all()

    with store_db.connect() as conn:
        row = store_db.fetch_one(conn, "SELECT incident_id FROM signals")
        inc = store_db.fetch_one(conn, "SELECT state FROM incidents")

    assert row["incident_id"] == incident
    assert inc["state"] == "open"


def test_a_diagnosis_reaches_the_store(buffer, store_db):
    """Created during an incident, which is when the store is most likely to be
    unreachable — hence local-first through the outbox rather than a direct write."""
    with lz.connect() as conn:
        incident = lz.open_incident(conn, CLUSTER, "prod", trigger="manual")
        lz.record_diagnosis(
            conn, incident, model="test", bundle_sha256="abc",
            hypotheses=[{"cause": "oom"}], commands=[{"cmd": "kubectl top pod"}],
        )

    shipper.ship_all()

    with store_db.connect() as conn:
        diag = store_db.fetch_one(conn, "SELECT * FROM diagnoses")
        inc = store_db.fetch_one(conn, "SELECT state FROM incidents")

    assert diag["hypotheses"] == [{"cause": "oom"}]
    # The incident was re-queued because its state moved. A store holding a diagnosis
    # against an incident still marked 'open' would contradict itself.
    assert inc["state"] == "diagnosed"


# ---- at-least-once ---------------------------------------------------------


def test_reshipping_after_a_crash_is_a_no_op(buffer, store_db):
    """Simulates the exact crash window: the store committed, the outbox had not yet
    been cleared. Re-sending is the ordinary recovery path, and the upsert absorbs it."""
    collect([make_signal()])

    with lz.connect() as conn:
        _, batch = lz.pending(conn, 100)

    # Ship normally, then put the same entries back as a crash would have left them.
    shipper.ship_once()
    with lz.connect() as conn:
        lz.enqueue(conn, "signal", batch["signal"])

    status, _ = shipper.ship_once()

    assert status is SourceStatus.OK
    assert stored_signals(store_db) == 1


def test_repeated_collection_ships_one_row(buffer, store_db):
    """Eight outbox entries, one row on the wire. Collapsing on read is what keeps a
    quiet cluster from generating shipping traffic proportional to poll frequency."""
    sig = make_signal()
    for _ in range(8):
        collect([sig])

    shipper.ship_once()

    assert stored_signals(store_db) == 1


# ---- the store is down -----------------------------------------------------


def _break_store(monkeypatch):
    """Point the pool at a port nothing is listening on. Closing the existing pool is
    the load-bearing half: it is created once per process and would otherwise keep
    serving healthy connections built from the old DSN."""
    store_connection.close()
    monkeypatch.setattr(
        config, "STORE_DSN", "host=127.0.0.1 port=1 dbname=oncall user=oncall connect_timeout=1"
    )
    monkeypatch.setattr(config, "STORE_CONNECT_TIMEOUT", 1)
    monkeypatch.setattr(shipper, "_schema_ready", False)


def test_an_unreachable_store_leaves_the_batch_queued(buffer, store_db, monkeypatch):
    """The central claim of the whole design. Nothing is lost, the buffer keeps
    absorbing, and the next cycle retries — designed behaviour for a store outage
    rather than a failure of it."""
    collect([make_signal()])
    _break_store(monkeypatch)

    status, written = shipper.ship_once()

    assert (status, written) == (SourceStatus.UNAVAILABLE, 0)
    assert shipper.backlog() == 1

    store_connection.close()


def test_collection_is_untouched_while_the_store_is_down(buffer, store_db, monkeypatch):
    """Collectors never talk to the store. An outage costs the agent its history, never
    its evidence."""
    _break_store(monkeypatch)

    collect([make_signal(name=f"pod-{i}", key=f"app|Error|{i}") for i in range(5)])
    shipper.ship_once()

    with lz.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] == 5
        assert lz.depth(conn) == 5

    store_connection.close()


def test_an_outage_is_recorded_with_its_reason(buffer, store_db, monkeypatch):
    """A shipper that died quietly must not look like a shipper with nothing to do."""
    collect([make_signal()])
    _break_store(monkeypatch)
    shipper.ship_once()

    with lz.connect() as conn:
        run = lz.last_shipping_run(conn)

    assert run["status"] == "unavailable"
    assert run["error"]

    store_connection.close()


def test_a_backlog_back_fills_on_recovery(buffer, store_db, monkeypatch):
    """The recovery half. Everything buffered during the outage arrives once the store
    returns, including rows whose month may need a partition that does not exist yet."""
    _break_store(monkeypatch)
    collect([make_signal(name=f"pod-{i}", key=f"app|Error|{i}") for i in range(20)])
    assert shipper.ship_once()[0] is SourceStatus.UNAVAILABLE

    monkeypatch.undo()
    store_connection.close()

    status, written = shipper.ship_all()

    assert status is SourceStatus.OK
    assert written == 20
    assert shipper.backlog() == 0
    assert stored_signals(store_db) == 20


# ---- draining --------------------------------------------------------------


def test_a_deep_queue_drains_over_bounded_cycles(buffer, store_db):
    """Bounded transactions rather than one long one: a single statement covering a
    large backlog would hold the buffer's only write slot for its whole duration, and
    collectors would stall behind a catch-up."""
    collect([make_signal(name=f"pod-{i}", key=f"app|Error|{i}") for i in range(25)])

    status, written = shipper.ship_all()

    assert (status, written) == (SourceStatus.OK, 25)
    assert stored_signals(store_db) == 25


def test_a_queued_id_whose_row_was_dropped_does_not_wedge_the_queue(buffer, store_db):
    """The size backstop can delete an unshipped row. Leaving its outbox entry behind
    would make every future cycle look it up, find nothing, and never drain."""
    collect([make_signal()])

    with lz.connect() as conn:
        conn.execute("DELETE FROM signals")

    status, written = shipper.ship_once()

    assert (status, written) == (SourceStatus.OK, 0)
    assert shipper.backlog() == 0
