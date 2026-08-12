"""
Reads from the durable store.

  - Deliberately not a copy of the buffer's reader. The two tiers answer different
    questions, so making one SQL serve both would force the lowest common denominator
    and forfeit jsonb, window functions and partitions — half the reason the store
    exists.
  - This side answers questions about history: has this happened before, is it getting
    worse, does it happen on other clusters. The buffer answers what is happening now,
    and it is the one the assembler reads on the critical path of a diagnosis.
  - Every function here can fail. An unreachable store must degrade the agent from
    "this is happening and it is new" to "this is happening" — never to nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection

from oncall.store.connection import fetch_all, fetch_one


def first_seen_ever(conn: Connection, fingerprint: str) -> datetime | None:
    """Turns "started after the deploy" into "has never happened before the deploy".

    Everything inside an incident window started after some deploy, so the weak form
    proves nothing. This lives here rather than in the buffer because the buffer holds
    two days: asked there it would answer "never seen before" for anything older, which
    is the most confident possible way to be wrong.

    No cluster filter. "Has this ever happened anywhere" is both a stronger claim and
    the more useful one once more than one cluster reports in.
    """
    row = fetch_one(
        conn,
        "SELECT MIN(event_time) AS first_seen FROM signals WHERE fingerprint = %s",
        (fingerprint,),
    )
    return row["first_seen"] if row else None


def is_new(conn: Connection, fingerprint: str, since: datetime) -> bool:
    """Whether a problem is genuinely new as of a moment, rather than merely newly
    noticed. Cheaper than first_seen_ever because it stops at the first row."""
    row = fetch_one(
        conn,
        "SELECT 1 AS hit FROM signals WHERE fingerprint = %s AND event_time < %s LIMIT 1",
        (fingerprint, since),
    )
    return row is None


def recurrence_history(
    conn: Connection, fingerprint: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Daily counts for one problem. Answers "is this getting worse", which needs more
    history than an incident window contains and is therefore a store question."""
    return fetch_all(
        conn,
        """
        SELECT date_trunc('day', event_time) AS day,
               COUNT(*)                      AS occurrences
        FROM signals
        WHERE fingerprint = %s AND event_time BETWEEN %s AND %s
        GROUP BY 1
        ORDER BY 1
        """,
        (fingerprint, start, end),
    )


def clusters_affected(
    conn: Connection, fingerprint: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Whether one problem is showing up in more than one place. A fingerprint carries
    no cluster of its own, so this is a plain grouping — the payoff for having decided
    identity from content rather than from location."""
    return fetch_all(
        conn,
        """
        SELECT cluster,
               COUNT(*)        AS occurrences,
               MIN(event_time) AS first_seen,
               MAX(event_time) AS last_seen
        FROM signals
        WHERE fingerprint = %s AND event_time BETWEEN %s AND %s
        GROUP BY cluster
        ORDER BY occurrences DESC
        """,
        (fingerprint, start, end),
    )


def signals_by_payload(
    conn: Connection,
    cluster: str,
    start: datetime,
    end: datetime,
    match: dict[str, Any],
) -> list[dict[str, Any]]:
    """Filter on fields the schema never knew about — exit_code, event_uid,
    waiting_reason. The @> containment operator uses the GIN index; in the buffer the
    same question is a full scan of every payload."""
    from psycopg.types.json import Jsonb

    return fetch_all(
        conn,
        """
        SELECT * FROM signals
        WHERE cluster = %s AND event_time BETWEEN %s AND %s AND payload @> %s
        ORDER BY event_time
        """,
        (cluster, start, end, Jsonb(match)),
    )


def signals_for_incident(conn: Connection, incident_id: str) -> list[dict[str, Any]]:
    """The eval corpus for one incident, read back whole. These rows are exempt from
    retention, which is what makes measuring whether diagnoses improve possible at all."""
    return fetch_all(
        conn,
        "SELECT * FROM signals WHERE incident_id = %s ORDER BY event_time",
        (incident_id,),
    )


def diagnoses_for_incident(conn: Connection, incident_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        "SELECT * FROM diagnoses WHERE incident_id = %s ORDER BY created_at",
        (incident_id,),
    )
