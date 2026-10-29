"""
What the UI shows, as plain data: everything here is testable without Streamlit.

  - One shape for a diagnosis whether it was just made or read back from the journal, so
    the live result and the history view cannot drift apart.
  - Citations are fingerprints in the journal. They are resolved against the bundle
    snapshot, which is the only record of what the model was shown; a fingerprint the
    snapshot lacks is shown as missing, never dropped.
  - A failed diagnosis gets a banner naming the outcome and its errors. An empty card
    would read as "nothing wrong".
  - unknowns None means the journal did not record them, which is different from a
    diagnosis that named no gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from oncall import config
from oncall.diagnosis.schema import DiagnosisRecord
from oncall.evidence.bundle import Bundle, Finding

NO_SNAPSHOT = "No bundle snapshot: cited evidence cannot be shown."

_BANNERS: dict[str, tuple[str, str]] = {
    "invalid_output": (
        "error",
        "No diagnosis: the model's answer failed validation, including after one repair.",
    ),
    "guard_rejected": (
        "error",
        "No diagnosis: the model suggested a command outside the read-only allowlist, so "
        "the whole answer was rejected.",
    ),
    "model_error": (
        "error",
        "No diagnosis: the Bedrock call failed. {bedrock_check} names the cause.",
    ),
    "prompt_too_large": (
        "error",
        "No diagnosis: the evidence exceeded the prompt cap, so nothing was sent.",
    ),
}


# The Bedrock preflight as the reader can run it where this UI is running: a make target
# in a checkout, the chart's test hook in the cluster, where there is no Makefile.
_BEDROCK_CHECK = {
    "local": "make -C oncall bedrock-check",
    "kubernetes": "helm test oncall -n oncall --logs",
}


def banner_for(outcome: str) -> tuple[str, str] | None:
    found = _BANNERS.get(outcome)
    if found is None:
        return None
    level, text = found
    return level, text.format(bedrock_check=_BEDROCK_CHECK[config.RUNTIME])


@dataclass(frozen=True)
class Evidence:
    fingerprint: str
    label: str
    found: bool
    source: str = ""
    kind: str = ""
    subject: str = ""
    state: str = ""
    severity: str = ""
    occurrences: int = 0
    sample: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    log_excerpt: str = ""


@dataclass(frozen=True)
class HypothesisView:
    rank: int
    cause_class: str
    title: str
    mechanism: str
    confidence: str
    supporting: list[Evidence]
    contradicting: list[Evidence]
    verify: list[dict[str, str]]
    # Empty for no_fault and for journal rows from before prompt v6.
    remediate: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DiagnosisView:
    diagnosis_id: str
    outcome: str
    banner: tuple[str, str] | None
    summary: str | None
    hypotheses: list[HypothesisView]
    unknowns: list[str] | None
    errors: list[str]
    warnings: list[str]
    receipt: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def first_command(self) -> str | None:
        """The fastest route to confirming the leading hypothesis."""
        if self.hypotheses and self.hypotheses[0].verify:
            return self.hypotheses[0].verify[0]["command"]
        return None



#--------------------------------------------------------------------------------------------------------
# BUILDERS
#--------------------------------------------------------------------------------------------------------



def row_from_record(record: DiagnosisRecord) -> dict[str, Any]:
    """A fresh record in the journal row's shape, so both go through build()."""
    d = record.diagnosis
    return {
        "diagnosis_id": record.diagnosis_id,
        "incident_id": record.incident_id,
        "created_at": record.created_at.isoformat().replace("+00:00", "Z"),
        "model": record.model,
        "prompt_version": record.prompt_version,
        "bundle_sha256": record.bundle_sha256,
        "prompt_sha256": record.prompt_sha256,
        "outcome": str(record.outcome),
        "attempts": record.attempts,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "latency_ms": record.latency_ms,
        "cost_usd": record.cost_usd,
        "summary": d.summary if d else None,
        "hypotheses": [h.model_dump(mode="json") for h in d.hypotheses] if d else [],
        "unknowns": list(d.unknowns) if d else None,
        "insufficient_evidence": d.insufficient_evidence if d else None,
        "errors": list(record.errors),
        "requested_by": record.requested_by,
    }


def build(row: dict[str, Any], bundle: Bundle | None, labels: dict[str, str]) -> DiagnosisView:
    """row as service.journalled() returns it; bundle None when no snapshot survives."""
    by_fp = {fp: label for label, fp in labels.items()}
    findings = {f.fingerprint: f for f in bundle.findings} if bundle else {}
    excerpts = {p.fingerprint: p.text for p in bundle.promoted} if bundle else {}

    def evidence(fp: str) -> Evidence:
        return _evidence(fp, by_fp.get(fp, fp[:8]), findings.get(fp), excerpts.get(fp, ""))

    outcome = row.get("outcome") or "ok"
    hypotheses = [
        HypothesisView(
            rank=h["rank"], cause_class=h["cause_class"], title=h["title"],
            mechanism=h["mechanism"], confidence=h["confidence"],
            supporting=[evidence(fp) for fp in h.get("supporting", [])],
            contradicting=[evidence(fp) for fp in h.get("contradicting", [])],
            verify=list(h.get("verify", [])),
            remediate=list(h.get("remediate") or []),
        )
        for h in row.get("hypotheses") or []
    ]

    banner = banner_for(outcome)
    if banner is None and row.get("insufficient_evidence"):
        banner = ("warning", "The model declined to name a cause; unknowns says what it lacked.")

    return DiagnosisView(
        diagnosis_id=row["diagnosis_id"],
        outcome=outcome,
        banner=banner,
        summary=row.get("summary"),
        hypotheses=hypotheses,
        unknowns=row.get("unknowns"),
        errors=list(row.get("errors") or []),
        warnings=warnings(bundle) if bundle else [NO_SNAPSHOT],
        receipt=receipt(row),
    )


RISK_LEVEL = {"low": "info", "medium": "warning", "high": "error"}


def evaluation_lines(evals: dict[str, dict[str, Any]]) -> list[str]:
    """One line per evaluator that has run, flags spelled out. Empty when none has."""
    from oncall.eval.online import FLAGS

    lines = []
    checks = evals.get("checks")
    if checks:
        d = checks["details"]
        score = f", score {checks['score']:.2f}" if checks.get("score") is not None else ""
        lines.append(f"checks: {checks['verdict']}{score}")
        lines += [f"  - {FLAGS.get(f, f)}" for f in d.get("flags", [])]
    outcome = evals.get("outcome")
    if outcome:
        d = outcome["details"]
        after = (f" after {d['resolved_after_minutes']} min"
                 if d.get("resolved_after_minutes") is not None else "")
        stable = {True: "; later diagnoses agreed", False: "; a later diagnosis disagreed",
                  None: ""}[d.get("cause_stable")]
        lines.append(f"outcome: {outcome['verdict']}{after}{stable}")
    human = evals.get("human")
    if human:
        cause = f", actual cause {human['cause_class']}" if human.get("cause_class") else ""
        remedy = human["details"].get("remedy")
        lines.append(f"engineer: {human['verdict']}{cause}"
                     + (f", remedy {remedy.replace('_', ' ')}" if remedy else ""))
    return lines


def remedy_heading(step: dict[str, Any]) -> str:
    """'medium risk: <action>', with the risk first so it is read before the action."""
    return f"{step.get('risk', 'unrated')} risk: {step.get('action', '')}"


def _evidence(fp: str, label: str, f: Finding | None, excerpt: str) -> Evidence:
    if f is None:
        return Evidence(fingerprint=fp, label=label, found=False)
    return Evidence(
        fingerprint=fp, label=label, found=True, source=str(f.source), kind=str(f.kind),
        subject=f.subject_name or f.owner_name or "", state=f.state or "",
        severity=str(f.severity) if f.severity else "", occurrences=f.occurrences,
        sample=f.sample or "",
        facts=dict(f.facts), log_excerpt=excerpt,
    )



#--------------------------------------------------------------------------------------------------------
# EVIDENCE HEALTH
#--------------------------------------------------------------------------------------------------------



def blind_sources(bundle: Bundle) -> list[str]:
    """Sources that could not see, phrased so absence never reads as health."""
    out = []
    for s in bundle.sources:
        if s.never_ran:
            out.append(f"{s.source} never ran in this window")
        elif str(s.status) != "ok":
            detail = f": {s.error}" if s.error else ""
            out.append(f"{s.source} was {s.status}{detail}")
    return out


def warnings(bundle: Bundle) -> list[str]:
    lost = [
        f"buffer dropped {loss.rows_dropped} rows ({loss.reason}), "
        f"{loss.unshipped} never reached the store"
        for loss in bundle.losses
    ]
    return [*bundle.degraded, *blind_sources(bundle), *lost]


def receipt(row: dict[str, Any]) -> dict[str, str]:
    cost = row.get("cost_usd")
    return {
        "outcome": row.get("outcome") or "ok",
        "model": str(row.get("model") or "-"),
        "prompt": f"v{row.get('prompt_version') or '-'}",
        "attempts": str(row.get("attempts") if row.get("attempts") is not None else "-"),
        "requested by": str(row.get("requested_by") or "-"),
        "latency": f"{(row.get('latency_ms') or 0) / 1000:.1f} s",
        "tokens in/out": f"{row.get('input_tokens') or 0:,} / {row.get('output_tokens') or 0:,}",
        "cost": f"${cost:.4f}" if cost is not None else "not priced",
        "diagnosis": str(row["diagnosis_id"]),
        "evidence receipt": str(row.get("bundle_sha256") or "-"),
        "prompt receipt": str(row.get("prompt_sha256") or "nothing sent"),
    }


def age(started_at: str | None, now: datetime | None = None) -> str:
    """'4 min ago' for a source-health row; 'never' when there is no run."""
    if not started_at:
        return "never"
    then = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    seconds = int(((now or datetime.now(UTC)) - then).total_seconds())
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"
