"""
Buffer retention tests.

Two rules, pulling against each other, and the tests exist to keep the tension honest.

The age sweep must never delete something the store has not got — that is ordinary
cleanup turning into silent loss the moment the store goes down. The size backstop
must be willing to, because the agent runs inside the cluster it diagnoses and a
buffer that fills the volume takes the agent down during the incident it exists to
explain. What makes the second acceptable is that it is recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope import Owner, Signal, SignalKind, SignalSource, Subject

CLUSTER = "test-cluster"
T0 = datetime(2026, 8, 12, 3, 14, 0, tzinfo=UTC)


def make_signal(name: str, when: datetime = T0) -> Signal:
    return Signal(
        source=SignalSource.K8S_PODS,
        kind=SignalKind.POD_STATE,
        cluster=CLUSTER,
        namespace="prod",
        event_time=when,
        subject=Subject(kind="Pod", name=name),
        owner=Owner(kind="Deployment", name="api"),
        dedupe_key=f"app|Error|{name}",
        payload={"exit_code": 1, "filler": "x" * 200},
    )


def write(signals: list[Signal], ship: bool = False) -> None:
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, signals)
        if ship:
            watermark, _ = lz.pending(conn, 10_000)
            lz.clear_through(conn, watermark)


def count() -> int:
    with lz.connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]


# ---- the age policy --------------------------------------------------------


def test_shipped_rows_leave_once_they_are_old(buffer):
    write([make_signal("old")], ship=True)

    with lz.connect() as conn:
        result = lz.sweep_buffer(conn, now=datetime.now(UTC) + timedelta(days=3))

    assert result["aged_out"] == 1
    assert count() == 0


def test_unshipped_rows_are_never_aged_out(buffer):
    """The guard that matters. A store outage lasting longer than the buffer window
    must cost freshness, never evidence — the rows stay until they have been sent."""
    write([make_signal("unshipped")], ship=False)

    with lz.connect() as conn:
        result = lz.sweep_buffer(conn, now=datetime.now(UTC) + timedelta(days=400))

    assert result["aged_out"] == 0
    assert count() == 1


def test_recent_rows_stay_even_when_shipped(buffer):
    """The buffer is not a queue that empties on delivery. Recent evidence is what the
    assembler reads on the critical path, and it must be there whether or not the store
    happens to be reachable."""
    write([make_signal("recent")], ship=True)

    with lz.connect() as conn:
        lz.sweep_buffer(conn, now=datetime.now(UTC))

    assert count() == 1


def test_an_age_sweep_is_recorded_as_a_drop(buffer):
    """Even a safe drop is written down. A reader of buffer_drops should not have to
    infer which losses were harmless from a reason column being absent."""
    write([make_signal("old")], ship=True)

    with lz.connect() as conn:
        lz.sweep_buffer(conn, now=datetime.now(UTC) + timedelta(days=3))
        drops = conn.execute("SELECT * FROM buffer_drops").fetchall()

    assert [d["reason"] for d in drops] == ["age"]
    assert drops[0]["unshipped"] == 0


# ---- the size backstop -----------------------------------------------------


def test_the_backstop_drops_shipped_rows_first(buffer):
    """Cheapest possible loss before any real one: a shipped row is already safe in the
    store, an unshipped one is not."""
    write([make_signal(f"shipped-{i}") for i in range(30)], ship=True)
    write([make_signal(f"queued-{i}") for i in range(5)], ship=False)

    with lz.connect() as conn:
        lz.sweep_buffer(conn, now=T0, max_bytes=1)
        remaining = [
            r["subject_name"] for r in conn.execute("SELECT subject_name FROM signals")
        ]

    # Everything shipped went before anything queued was touched; what survives, if
    # anything, is queued.
    assert all(name.startswith("queued-") for name in remaining)


def test_dropping_unshipped_rows_is_recorded_as_such(buffer):
    """The point of the whole table. A lost window that reads downstream as a quiet
    period is the failure this project is organised against, so the count of rows the
    store never received is written down separately."""
    write([make_signal(f"queued-{i}") for i in range(10)], ship=False)

    with lz.connect() as conn:
        result = lz.sweep_buffer(conn, now=T0, max_bytes=1)
        drops = conn.execute(
            "SELECT * FROM buffer_drops WHERE reason = 'size'"
        ).fetchall()

    assert result["dropped_unshipped"] > 0
    assert sum(d["unshipped"] for d in drops) == result["dropped_unshipped"]
    assert all(d["window_start"] and d["window_end"] for d in drops)


def test_dropping_an_unshipped_row_removes_its_outbox_entry(buffer):
    """Otherwise the shipper looks up a row that no longer exists on every cycle,
    forever, and the queue never drains."""
    write([make_signal(f"queued-{i}") for i in range(10)], ship=False)

    with lz.connect() as conn:
        lz.sweep_buffer(conn, now=T0, max_bytes=1)
        remaining_ids = {
            r["signal_id"] for r in conn.execute("SELECT signal_id FROM signals")
        }
        queued_ids = lz.unshipped_signal_ids(conn)

    assert queued_ids <= remaining_ids


def test_the_backstop_leaves_a_buffer_under_its_ceiling_alone(buffer):
    write([make_signal("small")], ship=True)

    with lz.connect() as conn:
        result = lz.sweep_buffer(conn, now=T0, max_bytes=config.BUFFER_MAX_BYTES)

    assert result["dropped"] == 0
    assert count() == 1


def test_drops_are_reported_to_the_assembler_by_window(buffer):
    """How a gap reaches a diagnosis as a stated fact rather than as silence."""
    write([make_signal(f"queued-{i}") for i in range(10)], ship=False)

    with lz.connect() as conn:
        lz.sweep_buffer(conn, now=T0, max_bytes=1)
        overlapping = lz.buffer_drops_in_window(
            conn, T0 - timedelta(hours=1), T0 + timedelta(hours=1)
        )
        elsewhere = lz.buffer_drops_in_window(
            conn, T0 + timedelta(days=30), T0 + timedelta(days=31)
        )

    assert overlapping
    assert not elsewhere


# ---- reclaiming ------------------------------------------------------------


def test_deleted_pages_can_be_returned_to_the_filesystem(buffer):
    """SQLite moves deleted pages onto a freelist rather than shrinking the file, so a
    rolling buffer would grow forever while its row count stayed flat. auto_vacuum is
    set to INCREMENTAL in schema.sql before the first table exists, precisely so this
    can run in bounded steps instead of one blocking full VACUUM."""
    write([make_signal(f"pod-{i}") for i in range(200)], ship=True)

    with lz.connect() as conn:
        lz.sweep_buffer(conn, now=datetime.now(UTC) + timedelta(days=3))

    with lz.connect() as conn:
        free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        reclaimed = lz.reclaim(conn)
        free_after = conn.execute("PRAGMA freelist_count").fetchone()[0]

    assert free_before > 0
    assert reclaimed > 0
    assert free_after < free_before
