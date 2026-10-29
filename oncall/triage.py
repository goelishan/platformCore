"""
Continuous triage: the agent diagnoses subjects in trouble without being asked.

  - Reads the buffer only, like the UI. A candidate is a workload or pod with a signal at
    TRIAGE_MIN_SEVERITY or worse that has lasted TRIAGE_SETTLE_SECONDS and was still seen
    within TRIAGE_ACTIVE_SECONDS. Owner name when there is one, so five crashing replicas
    are one incident.
  - decide() is the whole policy, a pure function over what the journal says about the
    candidate's incident. A subject is diagnosed when it has no diagnosis yet, when new
    evidence has arrived since the last one, when the last good one is old and the
    problem persists, or when the last attempt failed and the retry interval has passed,
    at most TRIAGE_MAX_FAILURES times in a row.
  - Spend is capped per pass and per hour. When the cap bites, the subjects left over
    are reported with that reason, never dropped silently, and the worst go first.
  - Incidents close themselves once their subject has been quiet for
    TRIAGE_RESOLVE_QUIET_SECONDS, and only while the sources that would show it are
    fresh. A blind collector produces silence too, and closing an incident on that
    silence would record a recovery that never happened.
  - An incident opened by the Alertmanager webhook (trigger "alert:<name>") is diagnosed
    on the next pass with no settle period, whatever its severity: the rule's "for" has
    settled it, and someone decided it matters. Argo CD rows never become candidates.
  - Every diagnosis it makes goes through diagnosis.service, the path the UI uses, so an
    automatic diagnosis is journalled, snapshotted and evaluated exactly like one asked
    for by hand. The incident's trigger is "auto", which is how the two are told apart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import Any

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope.timefmt import iso

log = logging.getLogger(__name__)

AUTO = "auto"
ALERT_PREFIX = "alert:"

SEVERITY_RANK = {"warning": 1, "error": 2, "critical": 3}

# The sources whose silence can mean "nothing wrong". Pods and events are where every
# failure shape this agent knows first appears; logs are read because of them.
WITNESSES = config.LOG_UPSTREAM_SOURCES


@dataclass(frozen=True)
class Candidate:
    subject: str
    namespace: str | None
    worst: int
    first_bad: datetime
    last_bad: datetime
    signals: int


@dataclass(frozen=True)
class History:
    """What the journal says about the candidate's unresolved incident."""

    incident_id: str | None = None
    last_at: datetime | None = None
    last_outcome: str | None = None
    last_ok_at: datetime | None = None
    failures_in_a_row: int = 0
    new_evidence: bool = False
    trigger: str | None = None


@dataclass(frozen=True)
class Decision:
    diagnose: bool
    reason: str


@dataclass
class Report:
    diagnosed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    blind: list[str] = field(default_factory=list)



#--------------------------------------------------------------------------------------------------------
# POLICY
#--------------------------------------------------------------------------------------------------------



def decide(c: Candidate, h: History, now: datetime) -> Decision:
    # An Alertmanager alert already waited out its rule's "for" before it fired.
    if h.trigger and h.trigger.startswith(ALERT_PREFIX) and h.last_at is None:
        return Decision(True, f"alert fired: {h.trigger[len(ALERT_PREFIX):]}")
    if now - c.first_bad < timedelta(seconds=config.TRIAGE_SETTLE_SECONDS):
        return Decision(False, "settling: too new to tell from a transient")
    if now - c.last_bad > timedelta(seconds=config.TRIAGE_ACTIVE_SECONDS):
        return Decision(False, "no longer seen")
    if h.last_at is None:
        return Decision(True, "never diagnosed")

    if h.last_outcome not in (None, "ok"):
        if h.failures_in_a_row >= config.TRIAGE_MAX_FAILURES:
            return Decision(False, f"{h.failures_in_a_row} failed attempts in a row; "
                                   "left for a person")
        if now - h.last_at < timedelta(seconds=config.TRIAGE_RETRY_SECONDS):
            return Decision(False, f"last attempt {h.last_outcome}; waiting to retry")
        return Decision(True, f"retry after {h.last_outcome}")

    if h.new_evidence:
        return Decision(True, "new evidence since the last diagnosis")
    if h.last_ok_at and now - h.last_ok_at >= timedelta(seconds=config.TRIAGE_REDIAGNOSE_SECONDS):
        return Decision(True, "still failing an hour after the last diagnosis")
    return Decision(False, "diagnosed; nothing new since")



#--------------------------------------------------------------------------------------------------------
# READING THE BUFFER
#--------------------------------------------------------------------------------------------------------



_CANDIDATES_SQL = """
SELECT coalesce(owner_name, subject_name) AS name,
       namespace,
       max(CASE severity WHEN 'critical' THEN 3 WHEN 'error' THEN 2 WHEN 'warning' THEN 1
                         ELSE 0 END) AS worst,
       min(event_time) AS first_bad,
       max(event_time) AS last_bad,
       count(*) AS signals
FROM signals
WHERE cluster = ? AND event_time >= ?
  AND coalesce(owner_name, subject_name) IS NOT NULL
  -- Argo CD rows are context, joined to a workload's bundle by namespace. A Degraded
  -- Application's workloads fail on their own, and diagnosing both would pay twice.
  AND source != 'argocd'
  AND CASE severity WHEN 'critical' THEN 3 WHEN 'error' THEN 2 WHEN 'warning' THEN 1
                    ELSE 0 END >= ?
GROUP BY 1, 2
ORDER BY worst DESC, last_bad DESC
"""


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _floor() -> int:
    try:
        return SEVERITY_RANK[config.TRIAGE_MIN_SEVERITY]
    except KeyError:
        raise RuntimeError(
            f"ONCALL_TRIAGE_MIN_SEVERITY must be one of {', '.join(SEVERITY_RANK)}, "
            f"not {config.TRIAGE_MIN_SEVERITY!r}"
        ) from None


def candidates(conn: Connection, cluster: str, now: datetime) -> list[Candidate]:
    """Subjects with a bad signal inside the lookback, worst and most recent first.

    The lookback is the bundle's own window, so first_bad is "first seen in the evidence
    a diagnosis would read", not first seen ever."""
    since = now - timedelta(seconds=config.BUNDLE_LOOKBACK_SECONDS)
    rows = conn.execute(_CANDIDATES_SQL, (cluster, iso(since), _floor())).fetchall()
    return [
        Candidate(r["name"], r["namespace"], r["worst"], _parse(r["first_bad"]),
                  _parse(r["last_bad"]), r["signals"])
        for r in rows
    ]


def history(conn: Connection, cluster: str, c: Candidate) -> History:
    incident = lz.unresolved_incident(conn, cluster, c.subject)
    if incident is None:
        return History()
    trigger = conn.execute("SELECT trigger FROM incidents WHERE incident_id = ?",
                           (incident,)).fetchone()["trigger"]
    rows = conn.execute(
        "SELECT created_at, outcome FROM diagnoses WHERE incident_id = ? "
        "ORDER BY created_at DESC",
        (incident,),
    ).fetchall()
    if not rows:
        return History(incident_id=incident, trigger=trigger)

    failures = 0
    for r in rows:
        if r["outcome"] in (None, "ok"):
            break
        failures += 1
    last_ok = next((_parse(r["created_at"]) for r in rows if r["outcome"] in (None, "ok")), None)
    last_at = _parse(rows[0]["created_at"])
    return History(
        incident_id=incident,
        trigger=trigger,
        last_at=last_at,
        last_outcome=rows[0]["outcome"],
        last_ok_at=last_ok,
        failures_in_a_row=failures,
        new_evidence=_new_since(conn, cluster, c, last_at),
    )


def _new_since(conn: Connection, cluster: str, c: Candidate, since: datetime) -> bool:
    """Whether a problem fingerprint first appeared after the last diagnosis. A repeat of
    a known one is not new evidence; a different failure on the same subject is."""
    row = conn.execute(
        "SELECT 1 AS hit FROM signals "
        "WHERE cluster = ? AND coalesce(owner_name, subject_name) = ? "
        "  AND CASE severity WHEN 'critical' THEN 3 WHEN 'error' THEN 2 "
        "                    WHEN 'warning' THEN 1 ELSE 0 END >= ? "
        "GROUP BY fingerprint HAVING min(event_time) > ? LIMIT 1",
        (cluster, c.subject, _floor(), iso(since)),
    ).fetchone()
    return row is not None


def alert_incidents(conn: Connection, cluster: str, now: datetime) -> list[Candidate]:
    """Incidents an alert opened that nothing has diagnosed yet, as candidates. Their
    subject may sit below the severity floor (a warning alert) or have no other rows, and
    is diagnosed anyway: someone wrote a rule saying this matters."""
    rows = conn.execute(
        "SELECT i.title, i.namespace FROM incidents i WHERE i.cluster = ? "
        "AND i.state != 'resolved' AND i.trigger LIKE ? AND i.title IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM diagnoses d WHERE d.incident_id = i.incident_id) "
        "ORDER BY i.opened_at",
        (cluster, f"{ALERT_PREFIX}%"),
    ).fetchall()
    return [Candidate(r["title"], r["namespace"], 1, now, now, 0) for r in rows]


def auto_diagnoses_since(conn: Connection, cluster: str, since: datetime) -> int:
    """Calls made by this loop, failures included: a failed call is still a paid one.
    By requested_by, not by the incident's trigger: the loop also diagnoses incidents a
    person opened, and those calls are spent from the same budget."""
    row = conn.execute(
        "SELECT count(*) AS n FROM diagnoses d JOIN incidents i USING (incident_id) "
        "WHERE i.cluster = ? AND d.requested_by = ? AND d.created_at >= ? "
        "AND d.attempts > 0",
        (cluster, AUTO, iso(since)),
    ).fetchone()
    return row["n"]


def blind_witnesses(conn: Connection, now: datetime) -> list[str]:
    """Witness sources whose newest run is missing, unfinished, failed or stale."""
    stale = now - timedelta(seconds=config.BUNDLE_STALE_SECONDS)
    blind = []
    for source in WITNESSES:
        run = lz.last_run(conn, source)
        if (run is None or run["finished_at"] is None or run["status"] == "unavailable"
                or _parse(run["started_at"]) < stale):
            blind.append(source)
    return blind



#--------------------------------------------------------------------------------------------------------
# RESOLUTION
#--------------------------------------------------------------------------------------------------------



def resolve_quiet(conn: Connection, cluster: str, now: datetime) -> list[str]:
    """Close every unresolved incident whose subject has been quiet long enough.

    The caller has already checked the witnesses. An incident younger than the quiet
    period is left alone even if its subject is quiet: it was opened by someone looking
    at something, possibly a problem the collectors do not see.
    """
    quiet_since = now - timedelta(seconds=config.TRIAGE_RESOLVE_QUIET_SECONDS)
    floor = _floor()
    closed = []
    for inc in conn.execute(
        "SELECT incident_id, title, opened_at FROM incidents "
        "WHERE cluster = ? AND state != 'resolved' AND opened_at < ?",
        (cluster, iso(quiet_since)),
    ).fetchall():
        subject_clause = ("AND coalesce(owner_name, subject_name) = ?"
                          if inc["title"] is not None else "")
        params: list[Any] = [cluster, iso(quiet_since), floor]
        if inc["title"] is not None:
            params.append(inc["title"])
        still = conn.execute(
            "SELECT 1 FROM signals WHERE cluster = ? AND event_time >= ? "
            "AND CASE severity WHEN 'critical' THEN 3 WHEN 'error' THEN 2 "
            "                  WHEN 'warning' THEN 1 ELSE 0 END >= ? "
            f"{subject_clause} LIMIT 1",
            params,
        ).fetchone()
        if still is None:
            lz.close_incident(conn, inc["incident_id"], state="resolved")
            closed.append(inc["incident_id"])
    return closed



#--------------------------------------------------------------------------------------------------------
# ONE PASS
#--------------------------------------------------------------------------------------------------------



def run_once(
    now: datetime | None = None,
    client: Any = None,
    diagnose: Callable[..., Any] | None = None,
    cluster: str | None = None,
) -> Report:
    """One triage pass. diagnose defaults to service.assemble then service.diagnose."""
    from oncall.diagnosis import service

    now = now or datetime.now(UTC)
    cluster = cluster or config.CLUSTER_NAME
    report = Report()

    with lz.connect() as conn:
        report.blind = blind_witnesses(conn, now)
        found = candidates(conn, cluster, now)
        known = {c.subject for c in found}
        found += [c for c in alert_incidents(conn, cluster, now) if c.subject not in known]
        plans = [(c, history(conn, cluster, c)) for c in found]
        spent = auto_diagnoses_since(conn, cluster, now - timedelta(hours=1))

    budget = max(0, min(config.TRIAGE_MAX_PER_TICK, config.TRIAGE_MAX_PER_HOUR - spent))
    for c, h in plans:
        d = decide(c, h, now)
        if not d.diagnose:
            report.skipped.append((c.subject, d.reason))
            continue
        if budget == 0:
            report.skipped.append((c.subject, f"spend cap: {d.reason}"))
            continue
        budget -= 1
        try:
            if diagnose is not None:
                outcome = diagnose(cluster, c.subject, now, client)
            else:
                b = service.assemble(cluster, c.subject, at=now)
                outcome = str(service.diagnose(b, cluster, c.subject, client=client,
                                               trigger=AUTO).record.outcome)
        except Exception as exc:  # noqa: BLE001 - one subject never stops the pass
            log.exception("triage of %s failed", c.subject)
            report.skipped.append((c.subject, f"raised {type(exc).__name__}"))
            continue
        report.diagnosed.append((c.subject, str(outcome)))

    # Closing waits for sight. With a witness blind, a quiet subject is unobserved, not
    # recovered, and the incident stays open until the source is back.
    if not report.blind:
        with lz.connect() as conn:
            report.resolved = resolve_quiet(conn, cluster, now)
    return report
