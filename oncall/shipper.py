"""
Moves what the buffer holds into the durable store.

  - The only component that talks to both tiers, and the only one on the write path
    that touches the network. Collectors never do, which is the whole point: the agent
    diagnoses broken clusters, so writing evidence must not depend on anything that
    breaks when a cluster does.
  - Delivery is at-least-once. The buffer's outbox is cleared only after the store has
    committed, so a crash in between re-sends the batch — and every store write is an
    upsert keyed on a content-derived id, which makes a re-send a no-op. Exactly-once
    delivery is hard; at-least-once over an idempotent write is easy and looks the same
    from outside.
  - An unreachable store is an ordinary outcome, not an error. The batch stays queued,
    the run records why, and collection carries on untouched.
"""

from __future__ import annotations

import logging

from oncall import config, store
from oncall import landing_zone as lz
from oncall.envelope import SourceStatus
from oncall.store import rows as store_rows

log = logging.getLogger(__name__)

# SQLite's default limit on bound variables is 999. Batches are chunked below it rather
# than at it, so a future column added to a lookup cannot quietly cross the line.
_IN_CHUNK = 500

# Migrations run once per process, latched only on success — the same shape, and the
# same reasoning, as auth loading in the Kubernetes client. Latching before the call
# would let one bad startup leave the process permanently unable to ship.
_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    applied = store.migrate()
    if applied:
        log.info("applied store migrations: %s", ", ".join(applied))
    _schema_ready = True


def _fetch(conn, table: str, id_column: str, ids: list[str]) -> list:
    """Read current state for the queued ids.

    Current state, not a recorded delta. A row upserted eight times between cycles
    crosses the wire once carrying what it looks like now, which is all the store
    wants — it has no use for the intermediate versions and the outbox never kept them.
    """
    found = []
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i : i + _IN_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        found.extend(
            conn.execute(
                f"SELECT * FROM {table} WHERE {id_column} IN ({placeholders})", chunk
            ).fetchall()
        )
    return found


def ship_once(batch_size: int | None = None) -> tuple[SourceStatus, int]:
    """One shipping cycle. Returns (status, rows written).

    The run row is opened and committed before any work begins, so a process killed
    mid-flight leaves a run with no finished_at — a third state, distinct from both
    "nothing to send" and "the store was down".
    """
    limit = batch_size or config.SHIP_BATCH_SIZE

    with lz.connect() as conn:
        run_id = lz.start_shipping_run(conn)

    try:
        with lz.connect() as conn:
            watermark, batch = lz.pending(conn, limit)

            if not watermark:
                lz.finish_shipping_run(conn, run_id, SourceStatus.EMPTY, 0)
                return SourceStatus.EMPTY, 0

            # Read everything out of the buffer before opening the store transaction.
            # Holding both a SQLite read and a Postgres transaction across a network
            # round trip would make the buffer's single writer wait on the store, which
            # is the coupling this whole design exists to avoid.
            incidents = _fetch(conn, "incidents", "incident_id", batch.get("incident", []))
            signals = _fetch(conn, "signals", "signal_id", batch.get("signal", []))
            diagnoses = _fetch(conn, "diagnoses", "diagnosis_id", batch.get("diagnosis", []))

        _ensure_schema()

        # Incidents first: signals and diagnoses both carry a foreign key to them. The
        # outbox is read as a prefix in seq order, and an incident is always enqueued
        # before anything referencing it, so a batch can never contain the child
        # without the parent already stored or present here.
        with store.connect() as pg:
            written = store.upsert_incidents(
                pg, [store_rows.incident_from_buffer(r) for r in incidents]
            )
            written += store.upsert_signals(
                pg, [store_rows.signal_from_buffer(r) for r in signals]
            )
            written += store.upsert_diagnoses(
                pg, [store_rows.diagnosis_from_buffer(r) for r in diagnoses]
            )

        # Only now. Clearing first would lose the batch on a crash; clearing after
        # merely repeats it, and the upserts absorb a repeat.
        #
        # The watermark is cleared whole even when fewer rows came back than ids went
        # out. A queued id with no row behind it was dropped by the size backstop, and
        # leaving its entry would make every future cycle look it up and find nothing,
        # forever.
        with lz.connect() as conn:
            lz.clear_through(conn, watermark)
            lz.finish_shipping_run(conn, run_id, SourceStatus.OK, written)

        return SourceStatus.OK, written

    except Exception as exc:
        # The batch stays queued. Nothing is lost, the buffer keeps absorbing, and the
        # next cycle retries — which is the designed behaviour for a store outage, not
        # a failure of it.
        error = f"{type(exc).__name__}: {exc}"
        log.warning("shipping failed, batch stays queued: %s", error)

        with lz.connect() as conn:
            lz.finish_shipping_run(conn, run_id, SourceStatus.UNAVAILABLE, 0, error)

        return SourceStatus.UNAVAILABLE, 0


def ship_all(max_cycles: int = 100) -> tuple[SourceStatus, int]:
    """Drain the queue over several bounded cycles.

    Bounded transactions rather than one long one: a single statement covering a large
    backlog would hold the buffer's only write slot for its whole duration, and
    collectors would stall behind a catch-up. The cycle cap stops a pathological loop
    if rows somehow keep reappearing faster than they ship.
    """
    total = 0

    for _ in range(max_cycles):
        status, written = ship_once()

        if status is SourceStatus.UNAVAILABLE:
            return status, total
        if status is SourceStatus.EMPTY:
            return (SourceStatus.OK if total else SourceStatus.EMPTY), total

        total += written

    log.warning("shipping hit the cycle cap with %s rows sent; queue may still be deep", total)
    return SourceStatus.OK, total


def backlog() -> int:
    """Outbox depth. Rises while the store is unreachable and is the number worth
    alerting on — long before the size backstop starts dropping rows."""
    with lz.connect() as conn:
        return lz.depth(conn)
