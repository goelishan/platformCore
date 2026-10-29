"""
The diagnose flow as a library: what make diagnose and the UI both call.

  - assemble() builds the bundle for a subject from the buffer. preview() adds the prompt
    the model would be shown, without opening anything or calling anyone.
  - diagnose() opens or reuses the incident, attaches the bundle's candidate signals by
    id, saves the snapshot, calls the model and journals the record, failures included.
  - replay() reruns a journalled diagnosis on its own snapshot, so a different answer is
    the model's or the prompt's doing and nothing else's.
  - journalled() and recent() read the buffer first and the store second, where
    diagnoses outlive buffer retention.
  - Nothing here prints. The Bedrock call runs outside any buffer transaction: holding
    SQLite's write lock for a 15 s call would stall every collector mid-incident.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from oncall import landing_zone as lz
from oncall.diagnosis import prompt, reasoner, snapshots
from oncall.diagnosis.journal import journal
from oncall.diagnosis.schema import DiagnosisRecord
from oncall.evidence import bundle as assembler

log = logging.getLogger(__name__)

_JSON_COLUMNS = ("hypotheses", "commands", "errors")

# After a failed store read, the store is skipped for this long. Each attempt waits out
# the connect timeout, and the UI reads history on every rerun: without this, an
# unreachable store, the normal state during an incident, stalls every click.
STORE_RETRY_SECONDS = 30.0
_store_skipped_until = 0.0


@dataclass(frozen=True)
class Preview:
    bundle: assembler.Bundle
    prompt: prompt.Prompt | None
    too_large: str | None = None


@dataclass(frozen=True)
class Diagnosed:
    record: DiagnosisRecord
    bundle: assembler.Bundle
    incident_id: str
    reused: bool
    attached: int
    labels: dict[str, str]


@dataclass(frozen=True)
class Replayed:
    old: dict[str, Any]
    record: DiagnosisRecord
    bundle: assembler.Bundle



#--------------------------------------------------------------------------------------------------------
# DIAGNOSE
#--------------------------------------------------------------------------------------------------------



def assemble(cluster: str, subject: str | None, at: datetime | None = None) -> assembler.Bundle:
    with lz.connect() as conn:
        return assembler.assemble(conn, cluster, subject_name=subject, now=at)


def labels(b: assembler.Bundle) -> dict[str, str]:
    """F-label to fingerprint as the model saw them, or none when it was too large to send."""
    try:
        return prompt.build(b).labels
    except prompt.PromptTooLarge:
        return {}


def preview(b: assembler.Bundle) -> Preview:
    try:
        return Preview(b, prompt.build(b))
    except prompt.PromptTooLarge as e:
        return Preview(b, None, str(e))


def diagnose(b: assembler.Bundle, cluster: str, subject: str | None,
             client: Any = None, trigger: str = "manual") -> Diagnosed:
    """trigger names who asked: manual from the UI or make diagnose, auto from the
    agent's triage loop. It is recorded on a new incident only; a reused incident keeps
    the trigger it was opened with."""
    with lz.connect() as conn:
        incident = lz.unresolved_incident(conn, cluster, subject)
        reused = incident is not None
        if incident is None:
            incident = lz.open_incident(
                conn, cluster, b.scope.namespace if b.scope else None,
                trigger=trigger, title=subject,
            )
        attached = lz.attach_signal_ids(conn, incident, b.candidate_signal_ids)

    snapshots.save(b)
    with lz.connect() as conn:
        lz.queue_snapshot(conn, assembler.evidence_receipt(b))
    record = reasoner.diagnose(b, incident_id=incident, client=client)
    record = record.model_copy(update={"requested_by": trigger})
    with lz.connect() as conn:
        journal(conn, record)
    _evaluate(record.diagnosis_id)
    return Diagnosed(record, b, incident, reused, attached, labels(b))


def _evaluate(diagnosis_id: str) -> None:
    """The automatic checks, straight away, so the UI shows them beside the answer. A
    failure here never costs the diagnosis: the agent's evaluate job picks it up."""
    from oncall.eval import online

    try:
        online.evaluate_checks(diagnosis_id)
    except Exception:  # noqa: BLE001 - evaluation is never on the diagnosis's critical path
        log.exception("checks for %s failed; left for the evaluate job", diagnosis_id)


def replay(diagnosis_id: str, client: Any = None) -> Replayed:
    old = journalled(diagnosis_id)
    if old is None:
        raise LookupError(f"no diagnosis {diagnosis_id} in the buffer or the store")
    b = snapshots.load(old["bundle_sha256"])
    new = reasoner.diagnose(b, incident_id=old["incident_id"], client=client)
    new = new.model_copy(update={"requested_by": "manual"})
    with lz.connect() as conn:
        journal(conn, new)
    _evaluate(new.diagnosis_id)
    return Replayed(old, new, b)



#--------------------------------------------------------------------------------------------------------
# WHAT TO DIAGNOSE
#--------------------------------------------------------------------------------------------------------
#
# Candidates come from the buffer, never from the cluster: the UI sits above the landing zone.
# collect_now() is the one exception, and it goes through the collectors like make collect.



_SUBJECTS_SQL = """
SELECT coalesce(owner_name, subject_name) AS name,
       namespace,
       max(CASE severity WHEN 'critical' THEN 3 WHEN 'error' THEN 2 WHEN 'warning' THEN 1
                         ELSE 0 END) AS worst,
       count(*) AS signals,
       max(event_time) AS last_seen
FROM signals
WHERE cluster = ? AND event_time >= ? AND coalesce(owner_name, subject_name) IS NOT NULL
GROUP BY 1, 2
HAVING worst >= 1
ORDER BY worst DESC, last_seen DESC
LIMIT ?
"""

_SEVERITY = {3: "critical", 2: "error", 1: "warning"}


def subjects(cluster: str, since: datetime, limit: int = 50) -> list[dict[str, Any]]:
    """Workloads and pods with a warning or worse since `since`, worst first. Owner name
    when there is one, so five crashing replicas are one candidate, not five."""
    from oncall.envelope.timefmt import iso

    with lz.connect() as conn:
        rows = conn.execute(_SUBJECTS_SQL, (cluster, iso(since), limit)).fetchall()
    return [{**dict(r), "worst": _SEVERITY[r["worst"]]} for r in rows]


def source_health(cluster: str) -> list[dict[str, Any]]:
    """The newest collection run per source. A source missing here has never run."""
    with lz.connect() as conn:
        rows = conn.execute(
            "SELECT source, status, started_at, finished_at, signal_count, error "
            "FROM collection_runs r WHERE cluster = ? AND started_at = ("
            "  SELECT max(started_at) FROM collection_runs "
            "  WHERE source = r.source AND cluster = r.cluster) ORDER BY source",
            (cluster,),
        ).fetchall()
    return [dict(r) for r in rows]


def open_incidents(cluster: str, limit: int = 20) -> list[dict[str, Any]]:
    """Unresolved incidents, newest first, with who opened them and how many attempts."""
    with lz.connect() as conn:
        rows = conn.execute(
            "SELECT i.incident_id, i.title, i.trigger, i.state, i.opened_at, "
            "       count(d.diagnosis_id) AS diagnoses "
            "FROM incidents i LEFT JOIN diagnoses d USING (incident_id) "
            "WHERE i.cluster = ? AND i.state != 'resolved' "
            "GROUP BY i.incident_id ORDER BY i.opened_at DESC LIMIT ?",
            (cluster, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def collect_now() -> dict[str, str]:
    """One cycle of every registered collector, as make collect runs it."""
    from oncall.collectors import registry

    lz.bootstrap()
    return {
        str(source): ("raised" if result is None else f"{result[0]} ({result[1]} signals)")
        for source, result in registry.run_all().items()
    }



#--------------------------------------------------------------------------------------------------------
# READ BACK
#--------------------------------------------------------------------------------------------------------
#
# Rows come back as plain dicts with JSON columns decoded, whichever tier they came from,
# so callers never branch on where a diagnosis was found.



def journalled(diagnosis_id: str) -> dict[str, Any] | None:
    with lz.connect() as conn:
        row = conn.execute(
            "SELECT * FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
        ).fetchone()
    if row is not None:
        return _decoded(dict(row))

    found = _from_store("SELECT * FROM diagnoses WHERE diagnosis_id = %s", (diagnosis_id,))
    return _decoded(found[0]) if found else None


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Newest first, buffer and store merged by diagnosis_id; the store fills in what the
    buffer has already swept. An unreachable store narrows the list, never fails it."""
    with lz.connect() as conn:
        rows = {
            r["diagnosis_id"]: _decoded(dict(r))
            for r in conn.execute(
                "SELECT d.*, i.title, i.cluster, i.state FROM diagnoses d "
                "JOIN incidents i USING (incident_id) ORDER BY d.created_at DESC LIMIT ?",
                (limit,),
            )
        }
    for r in _from_store(
        "SELECT d.*, i.title, i.cluster, i.state FROM diagnoses d "
        "JOIN incidents i USING (incident_id) ORDER BY d.created_at DESC LIMIT %s",
        (limit,),
    ):
        rows.setdefault(r["diagnosis_id"], _decoded(r))
    return sorted(rows.values(), key=lambda r: str(r["created_at"]), reverse=True)[:limit]


def _from_store(sql: str, params: tuple) -> list[dict[str, Any]]:
    global _store_skipped_until
    from oncall import store

    if time.monotonic() < _store_skipped_until:
        return []
    try:
        with store.connect() as conn:
            return [dict(r) for r in store.fetch_all(conn, sql, params)]
    except Exception as exc:  # noqa: BLE001 - the buffer is the answer when the store is not
        log.info("store not consulted for %.0f s: %s", STORE_RETRY_SECONDS, exc)
        _store_skipped_until = time.monotonic() + STORE_RETRY_SECONDS
        # A pool that failed to connect keeps reconnecting in the background until closed.
        store.close()
        return []


def _decoded(row: dict[str, Any]) -> dict[str, Any]:
    for column in _JSON_COLUMNS:
        value = row.get(column)
        if isinstance(value, str):
            row[column] = json.loads(value)
        elif value is None:
            row[column] = []
    # NULL and [] are different claims: not recorded, against nothing the model could not see.
    if isinstance(row.get("unknowns"), str):
        row["unknowns"] = json.loads(row["unknowns"])
    row.setdefault("unknowns", None)
    if row.get("insufficient_evidence") is not None:
        row["insufficient_evidence"] = bool(row["insufficient_evidence"])
    if row.get("cost_usd") is not None:
        row["cost_usd"] = float(row["cost_usd"])
    if isinstance(row.get("created_at"), datetime):
        row["created_at"] = row["created_at"].isoformat().replace("+00:00", "Z")
    return row
