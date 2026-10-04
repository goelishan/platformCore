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

from oncall.envelope import SourceStatus, iso

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
    """Every row that happened in the window or was still being observed in it.

    Ordered by event_time because the assembler builds a timeline, not a set.

    Membership is event_time OR collected_at. A snapshot source re-writes the same row
    on every poll while the state holds, moving collected_at forward and leaving
    event_time where the state began: a pod stuck in ImagePullBackOff keeps the Ready
    transition from the day it broke, a node condition its last_transition_time, a
    Service its creation time. Filtering on event_time alone dropped every one of them
    once the state was older than the window, so the longest-running faults were the
    ones a bundle could not see (found 2026-09-30: badimage, configerror and a critical
    control-plane node, all broken for three weeks, absent from every bundle).
    """
    clauses = [
        "SELECT * FROM signals WHERE cluster = ? "
        "AND (event_time BETWEEN ? AND ? OR collected_at BETWEEN ? AND ?)"
    ]
    params: list[Any] = [cluster, iso(start), iso(end), iso(start), iso(end)]

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


# ---- log targets -----------------------------------------------------------
# The query that makes log collection signal-driven rather than a sweep, so its cost
# scales with how much is broken instead of with how large the cluster is.
#
# Returned as rows rather than grouped in SQL. The fields the log collector needs to
# choose a container and a stream — container, waiting_reason, restart_count — live
# inside the payload JSON, and json_extract() is a compile-time option in SQLite. A
# query that silently returns nothing on a runtime built without JSON1 would blind this
# collector while every other source kept reporting ok, which is the exact failure this
# project is organised against. Grouping in Python makes a missing piece a NameError
# instead of an empty result set.


def log_targets(
    conn: Connection,
    cluster: str,
    start: datetime,
    end: datetime,
    severities: tuple[str, ...],
) -> list[sqlite3.Row]:
    """Pod-subject signals in the window at or above the trigger severity.

    Ordered most recent first, so a cap applied by the caller keeps the freshest
    evidence rather than an arbitrary slice.

    Log signals are excluded from being triggers. An excerpt about a broken pod is not
    itself a reason to go and read that pod's logs, and without the exclusion every
    cycle would re-trigger on its own output — the same self-feeding loop the agent's
    own namespace is excluded to prevent, arriving by a different route.

    Same membership rule as signals_in_window, and for the same reason: a pod stuck in
    one state keeps an old event_time while being re-observed every poll, and reading
    only event_time meant the logs of exactly those pods were never fetched.
    """
    placeholders = ", ".join("?" for _ in severities)
    return conn.execute(
        f"SELECT * FROM signals "
        f"WHERE cluster = ? "
        f"  AND (event_time BETWEEN ? AND ? OR collected_at BETWEEN ? AND ?) "
        f"  AND subject_kind = 'Pod' AND severity IN ({placeholders}) "
        f"  AND source != 'k8s_logs' "
        f"ORDER BY event_time DESC",
        [cluster, iso(start), iso(end), iso(start), iso(end), *severities],
    ).fetchall()


# ---- blast radius ----------------------------------------------------------


def spread_of(
    conn: Connection, fingerprint: str, start: datetime, end: datetime
) -> sqlite3.Row:
    """How widely one problem is occurring, independent of the subject being examined.

    Five pods failing across five nodes and five pods failing on one node are different
    diagnoses, and node was promoted out of the payload into a column precisely so that
    telling them apart is a GROUP BY. This is the query that promotion was for.

    No cluster filter and no subject filter: the question is whether the fingerprint is
    confined to what the caller happens to be looking at, and a query scoped to the
    subject could only ever answer yes.
    """
    return conn.execute(
        """
        SELECT COUNT(DISTINCT subject_name) AS subjects,
               COUNT(DISTINCT node_name)    AS nodes,
               COUNT(DISTINCT namespace)    AS namespaces,
               COUNT(DISTINCT cluster)      AS clusters
        FROM signals
        WHERE fingerprint = ? AND event_time BETWEEN ? AND ?
        """,
        (fingerprint, iso(start), iso(end)),
    ).fetchone()


# ---- collection attempts ---------------------------------------------------


def run_counts_in_window(
    conn: Connection, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    """Every attempt in the window per source, and how many of them failed to look.

    source_status_in_window answers with the newest run alone, which is the right shape
    for "is this source working now" and the wrong one for "was this source working
    while the incident was happening". A source that failed four times and then
    succeeded reports ok there and reports four failures here, and a bundle drawing
    conclusions from a quiet source needs the second answer.

    A run with no finished_at counts as failed. start_run stamps ok before the
    collector has done anything, so a collector that raised or was killed leaves a row
    claiming success with nothing behind it.
    """
    return conn.execute(
        """
        SELECT source,
               COUNT(*) AS attempts,
               SUM(CASE WHEN finished_at IS NULL OR status = ? THEN 1 ELSE 0 END)
                   AS failed
        FROM collection_runs
        WHERE started_at BETWEEN ? AND ?
        GROUP BY source
        """,
        (str(SourceStatus.UNAVAILABLE), iso(start), iso(end)),
    ).fetchall()


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


UNFINISHED_RUN = (
    "run started and never finished: still in progress, or the collector died"
)


def source_status_in_window(
    conn: Connection, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    """What each source managed during the window.

    status, error and signal_count are bare columns alongside MAX(started_at). SQLite
    resolves those from the row producing the maximum, which is exactly what is wanted
    here. Standard SQL forbids it and Postgres rejects it, so this query needs a window
    function if the landing zone ever moves.

    An unfinished newest run reads as unavailable. start_run stamps ok at insert, so a
    collector that raised or was killed would otherwise vouch for evidence it never
    gathered. Fixed here rather than in the runner because a SIGKILL never reaches an
    except block, and the open row is then the only record there is.
    """
    return conn.execute(
        """
        SELECT source,
               MAX(started_at) AS last_started,
               CASE WHEN finished_at IS NULL THEN ? ELSE status END AS status,
               CASE WHEN finished_at IS NULL THEN ? ELSE error END AS error,
               signal_count
        FROM collection_runs
        WHERE started_at BETWEEN ? AND ?
        GROUP BY source
        """,
        (str(SourceStatus.UNAVAILABLE), UNFINISHED_RUN, iso(start), iso(end)),
    ).fetchall()
