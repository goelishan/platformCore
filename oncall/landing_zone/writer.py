"""
Every write into the local buffer.

  - ON CONFLICT DO UPDATE, never INSERT OR REPLACE. Replace is a DELETE followed by an
    INSERT, so a re-collected signal would silently drop the incident_id back-filled
    onto it, and the retention sweep would then delete it as unattached evidence.
  - The DO UPDATE list is generated from COLLECTOR_COLUMNS, so incident_id is excluded
    structurally rather than by anyone remembering to leave it out.
  - Transactions belong to connection.connect(). A collector run is a snapshot, and a
    half-written snapshot is worse than none: the assembler cannot tell it apart from
    a genuinely quiet cluster.
  - Every write that the store needs also appends to the outbox, in the same
    transaction. Enqueuing here rather than in the shipper is what makes it impossible
    for a committed row to be unknown to the shipper, or for a rolled-back row to be
    promised to it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Any

from oncall.envelope import Signal, SignalSource, SourceStatus, iso
from oncall.landing_zone import outbox
from oncall.landing_zone.rows import COLLECTOR_COLUMNS, to_row


def _now() -> str:
    return iso(datetime.now(UTC))


# ---- collection runs -------------------------------------------------------
# The run carries availability, not the signal. An unavailable source produces zero
# signals, so without this table its outage is indistinguishable from a quiet cluster.
# Identity here is random, not content-derived: two runs of the same collector in the
# same second are genuinely two runs, and hashing their content would merge them.
#
# Runs are not shipped. They describe the health of the collectors attached to *this*
# buffer, and the queries that read them — last_run, source_status_in_window — are
# asked during an incident, when the store may be exactly what is unreachable.


def start_run(conn: Connection, source: SignalSource, cluster: str) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO collection_runs (run_id, source, cluster, started_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, str(source), cluster, _now(), str(SourceStatus.OK)),
    )
    return run_id


def finish_run(
    conn: Connection,
    run_id: str,
    status: SourceStatus,
    signal_count: int = 0,
    error: str | None = None,
) -> None:
    """A run left without finished_at is a collector that died mid-flight. That is a
    distinct state from a run that completed and found nothing."""
    conn.execute(
        "UPDATE collection_runs "
        "SET finished_at = ?, status = ?, signal_count = ?, error = ? "
        "WHERE run_id = ?",
        (_now(), str(status), signal_count, error, run_id),
    )


# ---- shipping runs ---------------------------------------------------------
# Same shape and same argument as collection_runs, one layer out: a shipper that died
# quietly must not look like a shipper with nothing to send.


def start_shipping_run(conn: Connection) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO shipping_runs (run_id, started_at, status) VALUES (?, ?, ?)",
        (run_id, _now(), str(SourceStatus.OK)),
    )
    return run_id


def finish_shipping_run(
    conn: Connection,
    run_id: str,
    status: SourceStatus,
    shipped: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE shipping_runs "
        "SET finished_at = ?, status = ?, shipped = ?, error = ? WHERE run_id = ?",
        (_now(), str(status), shipped, error, run_id),
    )


# ---- signals ---------------------------------------------------------------
# Column names are interpolated because they come from a constant tuple in this
# package; values never are. Generating the update list from COLLECTOR_COLUMNS is what
# makes the incident_id exclusion impossible to break by accident.


_ALL_COLUMNS = ("signal_id", *COLLECTOR_COLUMNS, "run_id", "expires_at")

_UPSERT_SIGNAL = f"""
INSERT INTO signals ({", ".join(_ALL_COLUMNS)})
VALUES ({", ".join(f":{c}" for c in _ALL_COLUMNS)})
ON CONFLICT(signal_id) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in COLLECTOR_COLUMNS)},
    run_id     = excluded.run_id,
    expires_at = excluded.expires_at
"""


def write_signals(conn: Connection, run_id: str | None, signals: list[Signal]) -> int:
    """Idempotent by signal_id, so overlapping collection windows are safe and are in
    fact deliberate: gaps between polls lose events, overlaps do not.

    collected_at and expires_at move forward on re-collection, which slides the expiry
    while a problem is still being observed. incident_id does not move, ever.

    Re-collecting an unchanged signal still enqueues it. The row's collected_at did
    move, so the store's copy is genuinely stale, and suppressing the enqueue would
    mean deciding here what the store already knows — which this layer cannot see.
    """
    if not signals:
        return 0

    conn.executemany(_UPSERT_SIGNAL, [to_row(s, run_id) for s in signals])
    outbox.enqueue(conn, outbox.SIGNAL, (s.signal_id for s in signals))
    return len(signals)


# ---- incidents -------------------------------------------------------------
# Signals are written continuously, long before anyone declares an incident, so
# incident_id is nullable and back-filled. Requiring it up front would mean only ever
# collecting data about problems already known, destroying the pre-incident history
# that usually contains the cause.
#
# Incidents and diagnoses go through the outbox rather than straight to the store,
# because they are created *during* an incident — which is when the store is most
# likely to be unreachable. Writing them directly would mean the one moment findings
# cannot be recorded is the moment there are findings.


def open_incident(
    conn: Connection,
    cluster: str,
    namespace: str | None,
    trigger: str,
    title: str | None = None,
) -> str:
    incident_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO incidents "
        "(incident_id, opened_at, cluster, namespace, trigger, title, state) "
        "VALUES (?, ?, ?, ?, ?, ?, 'open')",
        (incident_id, _now(), cluster, namespace, trigger, title),
    )
    outbox.enqueue(conn, outbox.INCIDENT, [incident_id])
    return incident_id


def attach_signals(
    conn: Connection,
    incident_id: str,
    cluster: str,
    start: datetime,
    end: datetime,
    namespace: str | None = None,
    owner_name: str | None = None,
) -> int:
    """Back-fill incident_id across a window that reaches back before the alert fired,
    because the cause is normally in the minutes before anyone noticed.

    The IS NULL guard stops a later incident stealing evidence already attached to an
    earlier one; overlapping incidents are normal during a cascading failure.

    Optional filters are appended only when supplied. The usual "(? IS NULL OR col = ?)"
    shortcut would make the predicate unresolvable at plan time and cost the index.

    The affected ids are selected before the update rather than returned by it. UPDATE
    ... RETURNING would be one statement, but it needs SQLite 3.35, and pinning a
    minimum engine version for a once-per-incident convenience is a poor trade. Both
    statements run inside the caller's transaction, so nothing can change between them.
    """
    where = ["WHERE incident_id IS NULL AND cluster = ? AND event_time BETWEEN ? AND ?"]
    params: list[Any] = [cluster, iso(start), iso(end)]

    if namespace is not None:
        where.append("AND namespace = ?")
        params.append(namespace)
    if owner_name is not None:
        where.append("AND owner_name = ?")
        params.append(owner_name)

    predicate = " ".join(where)

    affected = [
        r["signal_id"]
        for r in conn.execute(f"SELECT signal_id FROM signals {predicate}", params)
    ]
    if not affected:
        return 0

    conn.execute(f"UPDATE signals SET incident_id = ? {predicate}", [incident_id, *params])

    # These rows changed after they were last shipped, and the change is the one field
    # the store must not lose: incident_id is what exempts a signal from retention as
    # part of the eval corpus.
    outbox.enqueue(conn, outbox.SIGNAL, affected)
    return len(affected)


def close_incident(conn: Connection, incident_id: str, state: str = "resolved") -> None:
    conn.execute(
        "UPDATE incidents SET state = ?, closed_at = ? WHERE incident_id = ?",
        (state, _now(), incident_id),
    )
    outbox.enqueue(conn, outbox.INCIDENT, [incident_id])


# ---- diagnoses -------------------------------------------------------------
# Journalled with the bundle hash, so a verdict is permanently tied to the exact
# evidence that produced it. This table doubles as the eval corpus, which is why
# retention never touches signals attached to an incident.


def record_diagnosis(
    conn: Connection,
    incident_id: str,
    model: str,
    bundle_sha256: str,
    hypotheses: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
) -> str:
    diagnosis_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO diagnoses (diagnosis_id, incident_id, created_at, model, "
        "bundle_sha256, hypotheses, commands, input_tokens, output_tokens, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            diagnosis_id,
            incident_id,
            _now(),
            model,
            bundle_sha256,
            json.dumps(hypotheses),
            json.dumps(commands),
            input_tokens,
            output_tokens,
            latency_ms,
        ),
    )
    conn.execute(
        "UPDATE incidents SET state = 'diagnosed' WHERE incident_id = ?",
        (incident_id,),
    )

    # Both rows changed. The incident is enqueued too because its state moved, and a
    # store holding a diagnosis against an incident still marked 'open' is a store
    # that contradicts itself.
    outbox.enqueue(conn, outbox.DIAGNOSIS, [diagnosis_id])
    outbox.enqueue(conn, outbox.INCIDENT, [incident_id])
    return diagnosis_id


# ---- buffer drops ----------------------------------------------------------


def record_buffer_drop(
    conn: Connection,
    reason: str,
    rows_dropped: int,
    unshipped: int,
    window_start: str | None,
    window_end: str | None,
) -> None:
    """What the buffer gave up, and over which window.

    Written by the sweep, read by the assembler before it concludes anything from a
    gap. Without it a dropped window is indistinguishable from a quiet one, which is
    the same failure three-state SourceStatus exists to prevent, one layer down.
    """
    conn.execute(
        "INSERT INTO buffer_drops "
        "(dropped_at, reason, rows_dropped, unshipped, window_start, window_end) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), reason, rows_dropped, unshipped, window_start, window_end),
    )
