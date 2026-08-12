"""
Store retention.

  - Two mechanisms, because they cost different amounts. Dropping a whole partition is
    a metadata operation and reclaims space instantly; deleting expired rows inside a
    partition still has to be vacuumed. Coarse first, fine second.
  - A partition is only dropped when nothing inside it is attached to an incident.
    Those rows are the eval corpus, and losing them destroys the ability to measure
    whether diagnoses are getting better — the one thing that makes the reasoner
    improvable rather than merely present.
  - Nothing here runs on a collection or shipping path. Retention is maintenance, and
    maintenance must never be able to slow down the arrival of evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection

from oncall import config
from oncall.store.connection import fetch_all, fetch_one


def _oldest_retention() -> timedelta:
    """The most generous per-kind policy. A partition holds every kind mixed together,
    so it can only be dropped once the longest-lived thing in it has expired."""
    return max([*config.STORE_RETENTION.values(), config.STORE_RETENTION_DEFAULT])


def signal_partitions(conn: Connection) -> list[dict[str, Any]]:
    """Every partition with the range it covers, read from the catalogue rather than
    inferred from the name. The name is a convenience; the bound is the truth."""
    return fetch_all(
        conn,
        """
        SELECT c.relname AS name,
               pg_get_expr(c.relpartbound, c.oid) AS bound,
               pg_total_relation_size(c.oid)      AS bytes
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'signals'
        ORDER BY c.relname
        """,
    )


def drop_expired_partitions(
    conn: Connection, now: datetime | None = None
) -> list[str]:
    """Drop partitions entirely older than the longest retention and holding no
    incident evidence.

    The upper bound is read from the catalogue, so a partition is only dropped when
    every row it could contain is past retention. Comparing on the partition name would
    work until someone created one with a different granularity.
    """
    cutoff = (now or datetime.now(UTC)) - _oldest_retention()
    dropped: list[str] = []

    for part in signal_partitions(conn):
        # pg_get_expr renders the bound as
        #   FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00')
        # and the capture group excludes the quotes deliberately: including them yields
        # a string that still looks quoted, and the cast to timestamptz then fails.
        upper = fetch_one(
            conn,
            "SELECT (regexp_match(%s, 'TO \\(''([^'']+)''\\)'))[1]::timestamptz AS hi",
            (part["bound"],),
        )
        if not upper or upper["hi"] is None or upper["hi"] > cutoff:
            continue

        attached = fetch_one(
            conn,
            f'SELECT 1 AS hit FROM "{part["name"]}" WHERE incident_id IS NOT NULL LIMIT 1',
        )
        if attached:
            # Kept whole rather than partially emptied. A partition holding corpus rows
            # costs one table; deleting around them costs a vacuum and leaves the
            # partition anyway.
            continue

        conn.execute(f'DROP TABLE "{part["name"]}"')
        dropped.append(part["name"])

    return dropped


def sweep_expired(conn: Connection, now: datetime | None = None) -> int:
    """Per-kind expiry inside partitions that are still live.

    Needed because a partition spans a month while log excerpts live a day. Without it a
    January partition would keep its log excerpts until February.
    """
    cutoff = now or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM signals WHERE expires_at < %s AND incident_id IS NULL",
            (cutoff,),
        )
        return cur.rowcount


def sweep(conn: Connection, now: datetime | None = None) -> dict[str, Any]:
    """Coarse then fine. Dropping partitions first means the row-level delete has less
    to walk, and anything the drop removed never has to be vacuumed."""
    dropped = drop_expired_partitions(conn, now)
    return {"partitions_dropped": dropped, "rows_deleted": sweep_expired(conn, now)}
