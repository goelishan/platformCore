"""
Every read out of the landing zone. The assembler talks to this and to nothing else.

  - Deliberately narrow. A fixed set of questions means no caller invents ad-hoc SQL
    against a schema it does not own; anything missing is a design conversation.
  - recurrences() groups at read time. Occurrences are never collapsed on write,
    because the spacing between them separates exponential CrashLoopBackOff from a
    fixed-interval external killer, and first/last/count cannot tell those apart.
  - Optional filters are appended only when supplied, so every predicate stays real
    and the indexes stay usable.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from sqlite3 import Connection
from typing import Any

from oncall.envelope import iso

# ---- raw evidence ----------------------------------------------------------


def signals_in_window(
    conn: Connection,
    cluster: str,
    start: datetime,
    end: datetime,
    namespace: str | None = None,
    subject_name: str | None = None,
    owner_name: str | None = None,
) -> list[sqlite3.Row]:
    """Ordered by event_time because the assembler builds a timeline, not a set."""
    clauses = ["SELECT * FROM signals WHERE cluster = ? AND event_time BETWEEN ? AND ?"]
    params: list[Any] = [cluster, iso(start), iso(end)]

    if namespace is not None:
        clauses.append("AND namespace = ?")
        params.append(namespace)
    if subject_name is not None:
        clauses.append("AND subject_name = ?")
        params.append(subject_name)
    if owner_name is not None:
        clauses.append("AND owner_name = ?")
        params.append(owner_name)

    clauses.append("ORDER BY event_time")
    return conn.execute(" ".join(clauses), params).fetchall()


def signals_for_incident(conn: Connection, incident_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals WHERE incident_id = ? ORDER BY event_time",
        (incident_id,),
    ).fetchall()


# ---- recurrence ------------------------------------------------------------
# One row per distinct problem, assembled on demand. Storing first/last/count instead
# would be smaller and irreversibly lossy: a pod restarting on widening intervals and
# one restarting on a fixed cycle produce identical summaries and different causes.


def recurrences(
    conn: Connection,
    cluster: str,
    start: datetime,
    end: datetime,
    namespace: str | None = None,
) -> list[sqlite3.Row]:
    clauses = [
        """
        SELECT fingerprint,
               kind,
               subject_kind,
               subject_name,
               owner_name,
               MIN(event_time) AS first_seen,
               MAX(event_time) AS last_seen,
               COUNT(*)        AS occurrences
        FROM signals
        WHERE cluster = ? AND event_time BETWEEN ? AND ?
        """
    ]
    params: list[Any] = [cluster, iso(start), iso(end)]

    if namespace is not None:
        clauses.append("AND namespace = ?")
        params.append(namespace)

    clauses.append("GROUP BY fingerprint ORDER BY occurrences DESC, last_seen DESC")
    return conn.execute(" ".join(clauses), params).fetchall()


def occurrence_times(
    conn: Connection, fingerprint: str, start: datetime, end: datetime
) -> list[str]:
    """The individual timestamps behind a group. Widening gaps mean CrashLoopBackOff
    backing off; even gaps mean something external on a cycle."""
    rows = conn.execute(
        "SELECT event_time FROM signals WHERE fingerprint = ? "
        "AND event_time BETWEEN ? AND ? ORDER BY event_time",
        (fingerprint, iso(start), iso(end)),
    ).fetchall()
    return [r["event_time"] for r in rows]


# first_seen_ever lives in the store, not here. It asks whether a problem has ever
# happened before, and the buffer only holds the last couple of days — asked here it
# would answer "never" for anything older than the buffer window, which is the most
# confident possible way to be wrong. See store.reader.first_seen_ever.


# ---- what the buffer gave up -----------------------------------------------


def buffer_drops_in_window(
    conn: Connection, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    """Losses overlapping the window under examination.

    The assembler consults this before concluding anything from a gap. Without it a
    dropped window and a quiet window are the same shape, which is the failure the
    three-state source status exists to prevent, one tier down.
    """
    return conn.execute(
        "SELECT * FROM buffer_drops "
        "WHERE window_end >= ? AND window_start <= ? ORDER BY dropped_at",
        (iso(start), iso(end)),
    ).fetchall()


def last_shipping_run(conn: Connection) -> sqlite3.Row | None:
    """Staleness check for the shipper. A shipper that died leaves a growing outbox and
    a stale started_at, and nothing else in the system would notice."""
    return conn.execute(
        "SELECT * FROM shipping_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


# ---- source availability ---------------------------------------------------
# The only path by which absence reaches the prompt as a stated fact rather than as
# silence that reads like health. The assembler must consult this before concluding
# anything from missing evidence.


def last_run(conn: Connection, source: str) -> sqlite3.Row | None:
    """Staleness check. A collector that died silently leaves recent event_times and an
    old started_at, and nothing else in the system would notice."""
    return conn.execute(
        "SELECT * FROM collection_runs WHERE source = ? ORDER BY started_at DESC LIMIT 1",
        (source,),
    ).fetchone()


def source_status_in_window(
    conn: Connection, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    """What each source managed during the window.

    status, error and signal_count are bare columns alongside MAX(started_at). SQLite
    resolves those from the row producing the maximum, which is exactly what is wanted
    here. Standard SQL forbids it and Postgres rejects it, so this query needs a window
    function if the landing zone ever moves.
    """
    return conn.execute(
        """
        SELECT source,
               MAX(started_at) AS last_started,
               status,
               error,
               signal_count
        FROM collection_runs
        WHERE started_at BETWEEN ? AND ?
        GROUP BY source
        """,
        (iso(start), iso(end)),
    ).fetchall()
