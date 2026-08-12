"""
The transactional outbox: what the buffer still owes the store.

  - enqueue() is called inside the same transaction as the write it describes. That is
    the whole mechanism — the buffer cannot hold a row the shipper does not know about,
    and cannot promise a row whose transaction rolled back.
  - Delivery is at-least-once and that is deliberate. Exactly-once delivery is hard;
    at-least-once over an idempotent write is easy and indistinguishable from outside.
    The store upserts on signal_id, which is a hash of content rather than a sequence,
    so re-shipping the same row is a no-op. That property was decided in M1 and is what
    makes this design cheap.
  - Reads collapse by row_id. A signal upserted eight times between shipping cycles
    crosses the wire once, carrying its current state rather than eight deltas.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from sqlite3 import Connection

from oncall.envelope import iso

SIGNAL = "signal"
INCIDENT = "incident"
DIAGNOSIS = "diagnosis"


def enqueue(conn: Connection, kind: str, row_ids: Iterable[str]) -> int:
    """Append one entry per row. No deduplication on write.

    Checking for an existing pending entry first would be a read before every write on
    the hot path, and would save nothing: the reader collapses by row_id anyway, so a
    duplicate costs one row in a table that is emptied continuously.
    """
    now = iso(datetime.now(UTC))
    rows = [(kind, row_id, now) for row_id in row_ids]

    if not rows:
        return 0

    conn.executemany(
        "INSERT INTO outbox (kind, row_id, queued_at) VALUES (?, ?, ?)", rows
    )
    return len(rows)


def pending(conn: Connection, limit: int) -> tuple[int, dict[str, list[str]]]:
    """Returns (watermark, {kind: [row_id, ...]}).

    The watermark is the highest seq read, and it is what gets cleared after a
    successful ship. Rows are read in seq order so the batch is a prefix of the queue:
    clearing a prefix can never strand an older entry behind a newer one.

    A row_id appearing again beyond the watermark is not a problem. It ships a second
    time and the store upsert absorbs it.
    """
    rows = conn.execute(
        "SELECT seq, kind, row_id FROM outbox ORDER BY seq LIMIT ?", (limit,)
    ).fetchall()

    if not rows:
        return 0, {}

    batch: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    watermark = 0

    for row in rows:
        watermark = max(watermark, row["seq"])
        key = (row["kind"], row["row_id"])
        if key in seen:
            continue
        seen.add(key)
        batch.setdefault(row["kind"], []).append(row["row_id"])

    return watermark, batch


def clear_through(conn: Connection, watermark: int) -> int:
    """Called only after the store has acknowledged the batch.

    A crash between the store write and this call re-ships the batch on the next cycle,
    which is harmless. A crash in the other order would lose rows permanently, which is
    why the order is not an implementation detail.
    """
    return conn.execute("DELETE FROM outbox WHERE seq <= ?", (watermark,)).rowcount


def depth(conn: Connection) -> int:
    """How far behind the store is, in entries. Rises while the store is unreachable,
    and is the number worth alerting on before the size backstop starts dropping."""
    return conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]


def unshipped_signal_ids(conn: Connection) -> set[str]:
    """Signal ids the store has not acknowledged. The buffer sweep must not delete
    these, so it is the one guard that stands between a routine cleanup and silent
    data loss during a store outage."""
    rows = conn.execute(
        "SELECT DISTINCT row_id FROM outbox WHERE kind = ?", (SIGNAL,)
    ).fetchall()
    return {r["row_id"] for r in rows}
