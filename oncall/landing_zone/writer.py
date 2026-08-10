"""
Every write into the landing zone.

  - ON CONFLICT DO UPDATE, never INSERT OR REPLACE. Replace is a DELETE followed by an
    INSERT, so a re-collected signal would silently drop the incident_id back-filled
    onto it, and the retention sweep would then delete it as unattached evidence.
  - The DO UPDATE list is generated from COLLECTOR_COLUMNS, so incident_id is excluded
    structurally rather than by anyone remembering to leave it out.
  - Transactions belong to connection.connect(). A collector run is a snapshot, and a
    half-written snapshot is worse than none: the assembler cannot tell it apart from
    a genuinely quiet cluster.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Any

from oncall.envelope import Signal, SourceStatus, iso
from oncall.landing_zone.rows import COLLECTOR_COLUMNS, to_row


def _now() -> str:
    return iso(datetime.now(UTC))


# ---- collection runs -------------------------------------------------------
# The run carries availability, not the signal. An unavailable source produces zero
# signals, so without this table its outage is indistinguishable from a quiet cluster.
# Identity here is random, not content-derived: two runs of the same collector in the
# same second are genuinely two runs, and hashing their content would merge them.


def start_run(conn: Connection, source: str, cluster: str) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO collection_runs (run_id, source, cluster, started_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, source, cluster, _now(), str(SourceStatus.OK)),
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
    """
    if not signals:
        return 0

    conn.executemany(_UPSERT_SIGNAL, [to_row(s, run_id) for s in signals])
    return len(signals)


# ---- incidents -------------------------------------------------------------
# Signals are written continuously, long before anyone declares an incident, so
# incident_id is nullable and back-filled. Requiring it up front would mean only ever
# collecting data about problems already known, destroying the pre-incident history
# that usually contains the cause.


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
    """
    clauses = [
        "UPDATE signals SET incident_id = ?",
        "WHERE incident_id IS NULL AND cluster = ? AND event_time BETWEEN ? AND ?",
    ]
    params: list[Any] = [incident_id, cluster, iso(start), iso(end)]

    if namespace is not None:
        clauses.append("AND namespace = ?")
        params.append(namespace)
    if owner_name is not None:
        clauses.append("AND owner_name = ?")
        params.append(owner_name)

    return conn.execute(" ".join(clauses), params).rowcount


def close_incident(conn: Connection, incident_id: str, state: str = "resolved") -> None:
    conn.execute(
        "UPDATE incidents SET state = ?, closed_at = ? WHERE incident_id = ?",
        (state, _now(), incident_id),
    )


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
    return diagnosis_id
