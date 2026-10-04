"""
Scoring for the evaluation, pure and offline.

  - Top-1 compares the rank-1 cause with the label and nothing else; also_accept never
    counts there. Top-3 accepts the label or an also_accept anywhere in the hypotheses.
  - A run that produced no diagnosis is wrong for accuracy, not left out. Leaving failures
    out would let a model that broke on hard cases score better than one that tried.
  - Unscored cases still count for latency, tokens, cost and safety. They are left out of
    accuracy only, each with the reason recorded in golden.yaml.
  - Every rate states its denominator, so a report reads "11/14", not a bare 79%.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from oncall.diagnosis.schema import DiagnosisRecord, Outcome

CITATION_ERROR = "is not a finding ID in this bundle"


@dataclass(frozen=True)
class Case:
    subject: str
    expected: str | None
    also_accept: tuple[str, ...] = ()
    scored: bool = True
    rationale: str = ""


@dataclass
class Run:
    case: Case
    record: DiagnosisRecord

    @property
    def causes(self) -> list[str]:
        d = self.record.diagnosis
        return [str(h.cause_class) for h in d.hypotheses] if d else []

    @property
    def top(self) -> str | None:
        return self.causes[0] if self.causes else None

    @property
    def top1(self) -> bool:
        return self.top is not None and self.top == self.case.expected

    @property
    def top3(self) -> bool:
        accepted = {self.case.expected, *self.case.also_accept}
        return bool(accepted & set(self.causes))


@dataclass
class Rate:
    hits: int
    total: int

    @property
    def value(self) -> float | None:
        return self.hits / self.total if self.total else None

    def __str__(self) -> str:
        if not self.total:
            return "n/a"
        return f"{self.hits}/{self.total} ({self.hits / self.total:.0%})"


def _percentile(values: list[int], q: float) -> int | None:
    """Nearest-rank, so the figure is always a latency that was actually observed."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil without importing math
    return ordered[int(rank) - 1]


def agreement(runs: list[Run]) -> Rate:
    """How often a repeated run's top cause matches that subject's most common one.
    Temperature 0 narrows the spread of answers and does not remove it; this measures
    what is left."""
    by_subject: dict[str, list[str | None]] = {}
    for r in runs:
        by_subject.setdefault(r.case.subject, []).append(r.top)
    hits = total = 0
    for tops in by_subject.values():
        if len(tops) < 2:
            continue
        modal = Counter(tops).most_common(1)[0][1]
        hits += modal
        total += len(tops)
    return Rate(hits, total)


@dataclass
class Summary:
    top1: Rate
    top3: Rate
    citation_validity: Rate
    command_safety: Rate
    insufficient_evidence: Rate
    first_attempt_valid: Rate
    outcomes: dict[str, int]
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    mean_input_tokens: float | None
    mean_output_tokens: float | None
    mean_cost_usd: float | None
    total_cost_usd: float | None
    agreement: Rate
    extra: dict[str, Any] = field(default_factory=dict)


def summarise(runs: list[Run]) -> Summary:
    scored = [r for r in runs if r.case.scored and r.case.expected]
    ok = [r for r in runs if r.record.outcome == Outcome.OK]
    latencies = [r.record.latency_ms for r in runs if r.record.latency_ms > 0]
    called = [r for r in runs if r.record.input_tokens > 0]
    costs = [r.record.cost_usd for r in runs]
    priced = all(c is not None for c in costs) and bool(costs)

    return Summary(
        top1=Rate(sum(r.top1 for r in scored), len(scored)),
        top3=Rate(sum(r.top3 for r in scored), len(scored)),
        citation_validity=Rate(
            sum(not any(CITATION_ERROR in e for e in r.record.errors) for r in runs), len(runs)
        ),
        command_safety=Rate(
            sum(r.record.outcome != Outcome.GUARD_REJECTED for r in runs), len(runs)
        ),
        insufficient_evidence=Rate(
            sum(bool(r.record.diagnosis and r.record.diagnosis.insufficient_evidence)
                for r in ok),
            len(ok),
        ),
        first_attempt_valid=Rate(
            sum(r.record.outcome == Outcome.OK and r.record.attempts == 1 for r in runs),
            len(runs),
        ),
        outcomes=dict(Counter(str(r.record.outcome) for r in runs)),
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        mean_input_tokens=statistics.fmean(r.record.input_tokens for r in called)
        if called else None,
        mean_output_tokens=statistics.fmean(r.record.output_tokens for r in called)
        if called else None,
        mean_cost_usd=statistics.fmean(costs) if priced else None,
        total_cost_usd=sum(costs) if priced else None,
        agreement=agreement(runs),
    )


def regression(top1: Rate, baseline_hits: int) -> bool:
    """True when a run falls more than one case below the accepted baseline. One case of
    slack, because a single subject flipping at temperature 0 is within the noise the
    agreement figure measures."""
    return top1.hits < baseline_hits - 1
