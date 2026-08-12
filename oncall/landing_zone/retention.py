"""
Buffer retention.

  - The buffer is not the durable record, so its job is not to keep data — it is to
    hold data long enough to survive a store outage and no longer. Long-term policy
    lives in the store, driven by expires_at, which this file never consults.
  - Nothing unshipped is ever deleted by the age sweep. That single guard is what
    stands between routine cleanup and silent loss while the store is unreachable.
  - The size backstop may delete unshipped rows, because the agent runs inside the
    cluster it diagnoses and a buffer that fills the volume takes the agent down during
    the incident it exists to explain. Every such drop is recorded, with its window, so
    the gap reaches the assembler as a stated fact rather than as a quiet period.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import Any

from oncall import config
from oncall.envelope import iso
from oncall.landing_zone.writer import record_buffer_drop

# Rows removed per iteration of the size backstop. Small enough that a single statement
# stays short, large enough that draining a full buffer does not take thousands of
# round trips.
_DROP_CHUNK = 500

# Refuses to spin forever if deletion somehow fails to reduce the live size.
_MAX_DROP_ITERATIONS = 1000

_UNSHIPPED = (
    "EXISTS (SELECT 1 FROM outbox o WHERE o.kind = 'signal' AND o.row_id = s.signal_id)"
)


# ---- size ------------------------------------------------------------------
# Live bytes, not file bytes. Deleting rows in SQLite moves their pages onto the
# freelist rather than returning them to the filesystem, so the file size does not
# move until a vacuum runs. Measuring the file would make the backstop delete against
# space that is already free, and it would never stop.


def _live_bytes(conn: Connection) -> int:
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return (page_count - free) * page_size


def reclaim(conn: Connection, pages: int = 0) -> int:
    """Return freed pages to the filesystem.

    Kept separate from the sweep because it must run outside a transaction, and because
    it is the expensive half: the sweep is a delete, this rewrites file structure.

    Incremental rather than a full VACUUM, which would take an exclusive lock and copy
    the entire database. auto_vacuum = INCREMENTAL is set in schema.sql before the
    first table is created, since changing it afterwards requires exactly the full
    VACUUM this avoids.
    """
    before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.execute(f"PRAGMA incremental_vacuum({pages})" if pages else "PRAGMA incremental_vacuum")
    after = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return before - after


# ---- the sweep -------------------------------------------------------------


def _window_of(conn: Connection, signal_ids: list[str]) -> tuple[str | None, str | None]:
    """The event_time span about to be lost. Recorded rather than the collected_at span,
    because the assembler asks about when things happened, not when we looked."""
    placeholders = ", ".join("?" for _ in signal_ids)
    row = conn.execute(
        f"SELECT MIN(event_time) AS lo, MAX(event_time) AS hi "
        f"FROM signals WHERE signal_id IN ({placeholders})",
        signal_ids,
    ).fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)


def _delete(conn: Connection, signal_ids: list[str]) -> int:
    placeholders = ", ".join("?" for _ in signal_ids)
    return conn.execute(
        f"DELETE FROM signals WHERE signal_id IN ({placeholders})", signal_ids
    ).rowcount


def sweep_buffer(
    conn: Connection,
    now: datetime | None = None,
    retention: timedelta | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Age policy first, size backstop second. The now override exists so tests can
    freeze time rather than wait out a TTL.

    Returns what was removed and why. An empty result is the normal case.
    """
    moment = now or datetime.now(UTC)
    cutoff = iso(moment - (retention or config.BUFFER_RETENTION))
    ceiling = max_bytes if max_bytes is not None else config.BUFFER_MAX_BYTES

    result: dict[str, Any] = {"aged_out": 0, "dropped": 0, "dropped_unshipped": 0}

    # ---- age: shipped and past the buffer window ----------------------------
    aged = [
        r["signal_id"]
        for r in conn.execute(
            f"SELECT signal_id FROM signals s WHERE collected_at < ? AND NOT {_UNSHIPPED}",
            (cutoff,),
        )
    ]

    if aged:
        lo, hi = _window_of(conn, aged)
        result["aged_out"] = _delete(conn, aged)
        # Recorded like any other loss. This one is expected and harmless — the rows are
        # in the store — but a reader of buffer_drops should not have to infer which
        # drops were safe from the reason column being absent.
        record_buffer_drop(conn, "age", result["aged_out"], 0, lo, hi)

    # ---- size: the backstop -------------------------------------------------
    # Oldest first, and shipped rows before unshipped ones, so the cheapest possible
    # loss happens before any real one.
    for _ in range(_MAX_DROP_ITERATIONS):
        if _live_bytes(conn) <= ceiling:
            break

        batch = [
            r["signal_id"]
            for r in conn.execute(
                f"SELECT signal_id FROM signals s WHERE NOT {_UNSHIPPED} "
                f"ORDER BY collected_at LIMIT {_DROP_CHUNK}"
            )
        ]
        unshipped_batch: list[str] = []

        if not batch:
            # Nothing shipped is left to give up. Staying alive now costs real data, so
            # the oldest unshipped rows go and the loss is recorded as such.
            unshipped_batch = [
                r["signal_id"]
                for r in conn.execute(
                    f"SELECT signal_id FROM signals s WHERE {_UNSHIPPED} "
                    f"ORDER BY collected_at LIMIT {_DROP_CHUNK}"
                )
            ]
            batch = unshipped_batch

        if not batch:
            break

        lo, hi = _window_of(conn, batch)
        removed = _delete(conn, batch)

        if unshipped_batch:
            # The outbox entries have to go too. Leaving them would make the shipper
            # look up rows that no longer exist on every cycle, forever.
            placeholders = ", ".join("?" for _ in unshipped_batch)
            conn.execute(
                f"DELETE FROM outbox WHERE kind = 'signal' AND row_id IN ({placeholders})",
                unshipped_batch,
            )

        result["dropped"] += removed
        result["dropped_unshipped"] += len(unshipped_batch)
        record_buffer_drop(conn, "size", removed, len(unshipped_batch), lo, hi)

    return result


# ---- blobs -----------------------------------------------------------------
# Blob rows are deleted here; the files on disk are not. Unlinking belongs to the blob
# store in M3, so the orphaned paths are returned for the caller to remove.


# Defined once and used by both the SELECT that finds orphans and the DELETE that
# removes them. Two hand-written copies would eventually drift, and then rows would be
# deleted that were never inspected for their on-disk files.
_ORPHAN_BLOBS = (
    "expires_at < ? AND blob_id NOT IN "
    "(SELECT blob_id FROM signals WHERE blob_id IS NOT NULL)"
)


def sweep_blobs(conn: Connection, now: datetime | None = None) -> dict[str, Any]:
    cutoff = iso(now or datetime.now(UTC))

    orphan_paths = [
        r["path"]
        for r in conn.execute(f"SELECT path FROM blobs WHERE {_ORPHAN_BLOBS}", (cutoff,))
    ]
    deleted = conn.execute(f"DELETE FROM blobs WHERE {_ORPHAN_BLOBS}", (cutoff,)).rowcount

    return {"blobs": deleted, "orphan_paths": orphan_paths}
