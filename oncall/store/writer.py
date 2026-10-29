"""
Every write into the durable store.

  - All three writes are upserts, which is what turns the shipper's at-least-once
    delivery into effectively-once arrival. Exactly-once delivery is hard;
    at-least-once over an idempotent write is easy and indistinguishable from outside.
    Re-shipping a batch after a crash is therefore not an error path — it is the
    ordinary recovery path.
  - The conflict targets are content-derived identifiers, decided in M1. Two agent
    replicas watching one cluster produce identical signal_ids, so running more than
    one is safe by construction rather than by coordination.
  - Ordered incidents-first, then signals, then diagnoses, because signals and
    diagnoses both carry a foreign key to incidents.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from oncall.store.rows import DIAGNOSIS_COLUMNS, INCIDENT_COLUMNS, SIGNAL_COLUMNS


def _upsert(table: str, columns: tuple[str, ...], key: tuple[str, ...]) -> str:
    """Generated rather than written out, so a column added to the tuple in rows.py
    reaches the insert list and the update list together. Hand-maintained copies drift,
    and the failure is a column that silently stops being updated."""
    updatable = [c for c in columns if c not in key]
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('%(' + c + ')s' for c in columns)}) "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
    )


# event_time is in the conflict target because Postgres requires the partition key
# inside the unique constraint. It does not widen identity: signal_id is a hash of
# fingerprint and event_time, so the two can never disagree.
_UPSERT_SIGNAL = _upsert("signals", SIGNAL_COLUMNS, ("signal_id", "event_time"))
_UPSERT_INCIDENT = _upsert("incidents", INCIDENT_COLUMNS, ("incident_id",))
_UPSERT_DIAGNOSIS = _upsert("diagnoses", DIAGNOSIS_COLUMNS, ("diagnosis_id",))


def ensure_partitions(conn: Connection, rows: list[dict[str, Any]]) -> set[str]:
    """Create whatever monthly partitions this batch needs, before writing it.

    Driven by the data rather than by the calendar. A backlog that drained after a long
    store outage can carry rows from a previous month, and a scheduled "create next
    month" job would have no reason to have made that partition. Inserting into a
    partitioned table with no matching partition is a hard error, so the batch would
    fail permanently and retry forever.
    """
    months = {r["event_time"].replace(day=1, hour=0, minute=0, second=0, microsecond=0)
              for r in rows if r.get("event_time")}

    created: set[str] = set()
    for month in months:
        with conn.cursor() as cur:
            cur.execute("SELECT ensure_signal_partition(%s) AS part", (month,))
            row = cur.fetchone()
            if row:
                created.add(row["part"])
    return created


# DO NOTHING, never DO UPDATE: the first contact is the one fact about a cluster that a
# later write must not be able to move. See migration 0006.
_FIRST_CONTACT = (
    "INSERT INTO cluster_watch (cluster, first_contact_at) VALUES (%(cluster)s, %(at)s) "
    "ON CONFLICT (cluster) DO NOTHING"
)


def first_contacts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The earliest collected_at per cluster in one batch."""
    earliest: dict[str, Any] = {}
    for r in rows:
        at = r.get("collected_at")
        if at is not None and (r["cluster"] not in earliest or at < earliest[r["cluster"]]):
            earliest[r["cluster"]] = at
    return [{"cluster": c, "at": at} for c, at in sorted(earliest.items())]


def upsert_signals(conn: Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    ensure_partitions(conn, rows)
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SIGNAL, rows)
        cur.executemany(_FIRST_CONTACT, first_contacts(rows))
    return len(rows)


def upsert_incidents(conn: Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(_UPSERT_INCIDENT, rows)
    return len(rows)


def upsert_diagnoses(conn: Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(_UPSERT_DIAGNOSIS, rows)
    return len(rows)
