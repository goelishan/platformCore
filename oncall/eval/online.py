"""
Every diagnosis, evaluated where it was made: on real incidents, not on frozen lab faults.

  - Three evaluators, one row each per diagnosis (evaluations table):
      checks   automatic, as soon as a diagnosis is journalled. Deterministic questions
               the bundle can answer: did it cite the worst evidence, did it say what it
               could not see, is it confident while sources are blind, does it offer a
               fix, did it need a repair.
      outcome  automatic, once the incident has closed or OUTCOME_HORIZON has passed:
               did the problem stop, and did later diagnoses of the same incident agree.
      human    the engineer's verdict, the cause they actually found, and whether the
               remedy worked. The only evaluator that can say "right" or "wrong".
  - None of these is a model judging the model. A second call grading the first shares
    its blind spots and doubles the bill; the checks are rules a reviewer can read, the
    outcome is what the cluster did, and the verdict is a person's.
  - A check that cannot be applied is recorded as None, never as a pass. A diagnosis with
    no snapshot, or a bundle with no gaps to acknowledge, has not passed those checks;
    they did not apply, and the score is taken over the ones that did.
  - Versioned. A changed rule is a new CHECKS_VERSION, and pending() re-evaluates every
    diagnosis under it, so a report never mixes verdicts from two rule sets.

    python -m oncall.eval.online            # evaluate everything pending, once
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import Any

from oncall import landing_zone as lz
from oncall.diagnosis.schema import CauseClass
from oncall.envelope.timefmt import iso

log = logging.getLogger(__name__)

CHECKS = "checks"
OUTCOME = "outcome"
HUMAN = "human"

CHECKS_VERSION = "1"
OUTCOME_VERSION = "1"
HUMAN_VERSION = "1"

# How long after a diagnosis its outcome is judged if the incident has not closed. Two
# hours: past the triage loop's re-diagnosis interval, so a re-diagnosis has had its
# chance to happen and be compared.
OUTCOME_HORIZON = timedelta(hours=2)

HUMAN_VERDICTS = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}
REMEDY_RESULTS = ("worked", "did_not_work", "not_tried", "none_offered")

_SEVERITY = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_RISK = {"low": 0, "medium": 1, "high": 2}

# Flags, and what each one says about the diagnosis. The review report reads these.
FLAGS = {
    "no_diagnosis": "the call produced no diagnosis",
    "needed_repair": "the first answer broke a rule and had to be repaired",
    "worst_evidence_uncited": "no hypothesis cites the most severe finding in the bundle",
    "gaps_unacknowledged": "the bundle had evidence gaps and unknowns names none",
    "high_confidence_while_blind": "high confidence on rank 1 while a source was blind",
    "no_remedy": "the leading hypothesis names a fault and offers no fix",
    "riskiest_step_first": "the first remedy step is high risk while a lower-risk one is offered",
}


@dataclass(frozen=True)
class Evaluation:
    evaluator: str
    version: str
    verdict: str
    details: dict[str, Any]
    score: float | None = None
    cause_class: str | None = None
    notes: str | None = None



#--------------------------------------------------------------------------------------------------------
# CHECKS
#--------------------------------------------------------------------------------------------------------



def _gaps(bundle: Any) -> list[str]:
    """Everything the bundle says it could not see: degraded notes, blind sources and
    buffer losses. The same list the UI shows above a diagnosis."""
    out = list(bundle.degraded)
    for s in bundle.sources:
        if s.never_ran:
            out.append(f"{s.source} never ran")
        elif str(s.status) != "ok":
            out.append(f"{s.source} was {s.status}")
    out += [f"buffer dropped {loss.rows_dropped} rows" for loss in bundle.losses]
    return out


def _blind(bundle: Any) -> list[str]:
    return [s.source for s in bundle.sources if s.never_ran or str(s.status) == "unavailable"]


def checks(row: dict[str, Any], bundle: Any | None) -> Evaluation:
    """row as diagnosis.service.journalled() returns it; bundle None without a snapshot."""
    outcome = row.get("outcome") or "ok"
    hypotheses = row.get("hypotheses") or []
    leading = hypotheses[0] if hypotheses else None
    results: dict[str, bool | None] = {}
    details: dict[str, Any] = {
        "outcome": outcome,
        "attempts": row.get("attempts"),
        "requested_by": row.get("requested_by"),
        "snapshot": bundle is not None,
    }

    if outcome != "ok":
        details.update(flags=["no_diagnosis"], checks={"diagnosis": False})
        return Evaluation(CHECKS, CHECKS_VERSION, "fail", details, score=0.0)

    results["first_attempt"] = row.get("attempts") in (None, 1)
    cited = {fp for h in hypotheses for fp in (*h.get("supporting", []),
                                                *h.get("contradicting", []))}
    details.update(
        leading_cause=leading["cause_class"] if leading else None,
        confidence=leading["confidence"] if leading else None,
        hypotheses=len(hypotheses),
        citations=len(cited),
        insufficient_evidence=bool(row.get("insufficient_evidence")),
    )

    if bundle is not None:
        ranked = [(_SEVERITY.get(str(f.severity), -1), f.fingerprint) for f in bundle.findings
                  if f.severity is not None]
        worst = max((r for r, _ in ranked), default=-1)
        if worst >= _SEVERITY["warning"] and not row.get("insufficient_evidence"):
            results["worst_cited"] = any(fp in cited for r, fp in ranked if r == worst)
        else:
            results["worst_cited"] = None

        gaps = _gaps(bundle)
        details["gaps"] = gaps[:5]
        if gaps:
            results["gaps_acknowledged"] = bool(row.get("unknowns")) or bool(
                row.get("insufficient_evidence"))
        else:
            results["gaps_acknowledged"] = None

        blind = _blind(bundle)
        details["blind_sources"] = blind
        results["calibrated"] = (not (blind and leading and leading["confidence"] == "high")
                                 if leading else None)
    else:
        results.update(worst_cited=None, gaps_acknowledged=None, calibrated=None)

    steps = (leading or {}).get("remediate") or []
    details["remedy"] = {
        "steps": len(steps),
        "with_command": sum(1 for s in steps if s.get("command")),
        "max_risk": max((s.get("risk", "low") for s in steps), key=_RISK.get, default=None),
    }
    if leading and leading["cause_class"] != "no_fault":
        results["remedy_offered"] = bool(steps)
        risks = [_RISK.get(s.get("risk"), 0) for s in steps]
        results["least_risky_first"] = (not (risks and risks[0] == 2 and min(risks) < 2)
                                        if risks else None)
    else:
        results["remedy_offered"] = None
        results["least_risky_first"] = None

    flag_for = {
        "first_attempt": "needed_repair",
        "worst_cited": "worst_evidence_uncited",
        "gaps_acknowledged": "gaps_unacknowledged",
        "calibrated": "high_confidence_while_blind",
        "remedy_offered": "no_remedy",
        "least_risky_first": "riskiest_step_first",
    }
    flags = [flag_for[k] for k, v in results.items() if v is False]
    applied = [v for v in results.values() if v is not None]
    details.update(flags=flags, checks=results)
    score = round(sum(applied) / len(applied), 3) if applied else None
    return Evaluation(CHECKS, CHECKS_VERSION, "warn" if flags else "pass", details, score=score)



#--------------------------------------------------------------------------------------------------------
# OUTCOME
#--------------------------------------------------------------------------------------------------------



def outcome(row: dict[str, Any], incident: dict[str, Any], later: list[dict[str, Any]],
            now: datetime) -> Evaluation | None:
    """What happened after the diagnosis. None while it is too early to say.

    resolved means the incident closed: triage found the subject quiet with its witnesses
    fresh, or a person closed it. It does not mean the diagnosis was right. A restart can
    clear a fault nobody understood, and that is why cause_stable sits beside it: a
    diagnosis contradicted by the next one on the same incident is the weaker of the two.
    """
    created = _parse(row["created_at"])
    resolved = incident.get("state") == "resolved"
    if not resolved and now - created < OUTCOME_HORIZON:
        return None

    lead = (row.get("hypotheses") or [{}])[0].get("cause_class")
    later_causes = [((d.get("hypotheses") or [{}])[0]).get("cause_class") for d in later
                    if (d.get("outcome") or "ok") == "ok" and d.get("hypotheses")]
    details: dict[str, Any] = {
        "incident_state": incident.get("state"),
        "incident_trigger": incident.get("trigger"),
        "leading_cause": lead,
        "later_diagnoses": len(later),
        "later_causes": later_causes,
        "cause_stable": all(c == lead for c in later_causes) if later_causes else None,
    }
    if resolved and incident.get("closed_at"):
        minutes = (_parse(incident["closed_at"]) - created).total_seconds() / 60
        details["resolved_after_minutes"] = round(max(minutes, 0.0), 1)
    return Evaluation(OUTCOME, OUTCOME_VERSION, "resolved" if resolved else "persisted", details)



#--------------------------------------------------------------------------------------------------------
# HUMAN
#--------------------------------------------------------------------------------------------------------



def human(row: dict[str, Any], verdict: str, cause_class: str | None = None,
          remedy: str | None = None, notes: str | None = None) -> Evaluation:
    """The engineer's verdict. Refuses a contradiction rather than storing it: "wrong"
    with the model's own cause as the real one cannot both be true, and an eval corpus
    built on such a label would teach the opposite of what happened."""
    if verdict not in HUMAN_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(HUMAN_VERDICTS)}, not {verdict!r}")
    vocabulary = {c.value for c in CauseClass}
    if cause_class is not None and cause_class not in vocabulary:
        raise ValueError(f"cause {cause_class!r} is not a CauseClass")
    if remedy is not None and remedy not in REMEDY_RESULTS:
        raise ValueError(f"remedy must be one of {', '.join(REMEDY_RESULTS)}, not {remedy!r}")

    lead = (row.get("hypotheses") or [{}])[0].get("cause_class")
    if verdict == "correct" and cause_class is None:
        cause_class = lead
    if verdict == "wrong" and cause_class is not None and cause_class == lead:
        raise ValueError(f"verdict wrong, but the cause given is the model's own ({lead})")
    details = {
        "leading_cause": lead,
        "top1_match": (cause_class == lead) if cause_class and lead else None,
        "top3_match": (cause_class in [h.get("cause_class") for h in row.get("hypotheses") or []])
        if cause_class else None,
        "remedy": remedy,
    }
    return Evaluation(HUMAN, HUMAN_VERSION, verdict, details, score=HUMAN_VERDICTS[verdict],
                      cause_class=cause_class, notes=(notes or "").strip() or None)



#--------------------------------------------------------------------------------------------------------
# RECORDING
#--------------------------------------------------------------------------------------------------------



def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def record(conn: Connection, diagnosis_id: str, e: Evaluation) -> str:
    return lz.record_evaluation(
        conn, diagnosis_id, e.evaluator, e.version, e.verdict, e.details,
        score=e.score, cause_class=e.cause_class, notes=e.notes,
    )


def _row(conn: Connection, diagnosis_id: str) -> dict[str, Any] | None:
    from oncall.diagnosis.service import _decoded

    found = conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = ?",
                         (diagnosis_id,)).fetchone()
    return _decoded(dict(found)) if found else None


def _bundle(sha: str | None) -> Any | None:
    from oncall.diagnosis import snapshots

    if not sha:
        return None
    try:
        return snapshots.load(sha)
    except (FileNotFoundError, snapshots.SnapshotMismatch, ValueError):
        return None


def evaluate_checks(diagnosis_id: str) -> Evaluation | None:
    with lz.connect() as conn:
        row = _row(conn, diagnosis_id)
    if row is None:
        return None
    e = checks(row, _bundle(row.get("bundle_sha256")))
    with lz.connect() as conn:
        record(conn, diagnosis_id, e)
    return e


def record_human(diagnosis_id: str, verdict: str, cause_class: str | None = None,
                 remedy: str | None = None, notes: str | None = None) -> Evaluation:
    """From the UI or make feedback. The diagnosis must be in the buffer, which keeps
    every diagnosis it journalled."""
    with lz.connect() as conn:
        row = _row(conn, diagnosis_id)
        if row is None:
            raise LookupError(f"no diagnosis {diagnosis_id} in the buffer")
        e = human(row, verdict, cause_class, remedy, notes)
        record(conn, diagnosis_id, e)
    return e


def evaluations_for(diagnosis_id: str) -> dict[str, dict[str, Any]]:
    """Every evaluator's latest row for one diagnosis, details decoded, keyed by evaluator."""
    with lz.connect() as conn:
        rows = conn.execute("SELECT * FROM evaluations WHERE diagnosis_id = ?",
                            (diagnosis_id,)).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d["details"])
        out[d["evaluator"]] = d
    return out


_PENDING_CHECKS = """
SELECT d.diagnosis_id FROM diagnoses d
LEFT JOIN evaluations e
  ON e.diagnosis_id = d.diagnosis_id AND e.evaluator = ? AND e.evaluator_version = ?
WHERE e.evaluation_id IS NULL
ORDER BY d.created_at LIMIT ?
"""

_PENDING_OUTCOMES = """
SELECT d.diagnosis_id FROM diagnoses d
JOIN incidents i USING (incident_id)
LEFT JOIN evaluations e
  ON e.diagnosis_id = d.diagnosis_id AND e.evaluator = ? AND e.evaluator_version = ?
WHERE coalesce(d.outcome, 'ok') = 'ok'
  AND (i.state = 'resolved' OR d.created_at <= ?)
  -- Never judged, or judged "persisted" at the horizon and closed since.
  AND (e.evaluation_id IS NULL OR (e.verdict = 'persisted' AND i.state = 'resolved'))
ORDER BY d.created_at LIMIT ?
"""


def evaluate_pending(now: datetime | None = None, limit: int = 50) -> dict[str, int]:
    """Evaluate what is due. Bounded per call so a backlog drains over several passes."""
    now = now or datetime.now(UTC)
    done = {CHECKS: 0, OUTCOME: 0}

    with lz.connect() as conn:
        due_checks = [r["diagnosis_id"] for r in conn.execute(
            _PENDING_CHECKS, (CHECKS, CHECKS_VERSION, limit))]
    for diagnosis_id in due_checks:
        if evaluate_checks(diagnosis_id):
            done[CHECKS] += 1

    with lz.connect() as conn:
        due = [r["diagnosis_id"] for r in conn.execute(
            _PENDING_OUTCOMES, (OUTCOME, OUTCOME_VERSION, iso(now - OUTCOME_HORIZON), limit))]
        for diagnosis_id in due:
            row = _row(conn, diagnosis_id)
            incident = dict(conn.execute("SELECT * FROM incidents WHERE incident_id = ?",
                                         (row["incident_id"],)).fetchone())
            later = [_row(conn, r["diagnosis_id"]) for r in conn.execute(
                "SELECT diagnosis_id FROM diagnoses WHERE incident_id = ? AND created_at > ? "
                "ORDER BY created_at", (row["incident_id"], iso(_parse(row["created_at"]))))]
            e = outcome(row, incident, later, now)
            if e is not None:
                record(conn, diagnosis_id, e)
                done[OUTCOME] += 1
    return done


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Evaluate every pending diagnosis once.").parse_args(argv)
    lz.bootstrap()
    print(evaluate_pending())
    return 0


if __name__ == "__main__":
    sys.exit(main())
