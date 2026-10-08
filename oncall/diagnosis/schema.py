"""
The reasoner's output contract.

  - CauseClass is the closed vocabulary a hypothesis is scored against. It names a cause,
    never the place the cause surfaces: a stuck rollout, a Job out of retries and a Service
    with no endpoints are consequences, and the consequence belongs in title and mechanism.
  - Command, Hypothesis and Diagnosis are what the model returns through the
    record_diagnosis tool. tool_input_schema() is the JSON schema Bedrock enforces on it.
  - The schema constrains shape only: types, enum members, list bounds. Rules relating one
    field to another (contiguous ranks, a citation on both sides, the insufficient-evidence
    form) cannot be stated there, so the validators below run on every response.
  - DiagnosisRecord is one journal row per diagnose call, failures included. A journal that
    kept only successes could not measure a failure rate.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CauseClass(StrEnum):
    APP_CRASH = "app_crash"
    OOM_KILLED = "oom_killed"
    IMAGE_PULL = "image_pull"
    CONFIG_MISSING = "config_missing"
    UNSCHEDULABLE_RESOURCES = "unschedulable_resources"
    PVC_UNBOUND = "pvc_unbound"
    LIVENESS_PROBE = "liveness_probe"
    READINESS_PROBE = "readiness_probe"
    SELECTOR_MISMATCH = "selector_mismatch"
    TERMINATION_STUCK = "termination_stuck"
    NODE_UNSCHEDULABLE = "node_unschedulable"
    NO_FAULT = "no_fault"
    OTHER = "other"


# ---- model output ----------------------------------------------------------
# extra="forbid" so a field the model invents is an error rather than silently dropped.
# A citation is only a bounded string here; whether it exists in the bundle is a guard,
# because checking it needs the bundle and this module does not have one.

Text = Annotated[str, Field(min_length=1, max_length=2000)]
CitationId = Annotated[str, Field(min_length=1, max_length=64)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Command(_Strict):
    command: Annotated[str, Field(min_length=1, max_length=300)]
    purpose: Annotated[str, Field(min_length=1, max_length=300)]


class Hypothesis(_Strict):
    rank: Annotated[int, Field(ge=1, le=3)]
    cause_class: CauseClass
    title: Annotated[str, Field(min_length=1, max_length=120)]
    mechanism: Text
    confidence: Literal["low", "medium", "high"]
    supporting: Annotated[list[CitationId], Field(min_length=1, max_length=10)]
    contradicting: Annotated[list[CitationId], Field(max_length=10)] = []
    verify: Annotated[list[Command], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def _one_side_per_citation(self) -> Hypothesis:
        both = set(self.supporting) & set(self.contradicting)
        if both:
            raise ValueError(f"cited as both supporting and contradicting: {sorted(both)}")
        return self


class Diagnosis(_Strict):
    summary: Annotated[str, Field(min_length=1, max_length=600)]
    hypotheses: Annotated[list[Hypothesis], Field(max_length=3)]
    unknowns: Annotated[list[Text], Field(max_length=10)] = []
    insufficient_evidence: bool

    @model_validator(mode="after")
    def _shape(self) -> Diagnosis:
        # Declining to guess is a valid answer, and it has to say what was missing.
        if self.insufficient_evidence:
            if not self.unknowns:
                raise ValueError("insufficient_evidence requires at least one unknown")
        elif not self.hypotheses:
            raise ValueError("at least one hypothesis is required unless insufficient_evidence")

        ranks = [h.rank for h in self.hypotheses]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError(f"ranks must run 1..n in list order, got {ranks}")
        return self


def tool_input_schema() -> dict[str, Any]:
    """The record_diagnosis tool's inputSchema: shape only, see the module docstring."""
    return Diagnosis.model_json_schema()


# ---- journal record --------------------------------------------------------


class Outcome(StrEnum):
    OK = "ok"
    INVALID_OUTPUT = "invalid_output"
    GUARD_REJECTED = "guard_rejected"
    MODEL_ERROR = "model_error"
    # The evidence was over the prompt cap, so nothing was sent. Recorded rather than
    # raised: a bundle too large to diagnose is a failure rate worth watching, and the
    # journal cannot count what never reaches it.
    PROMPT_TOO_LARGE = "prompt_too_large"


class DiagnosisRecord(BaseModel):
    diagnosis_id: str
    incident_id: str
    created_at: datetime
    model: str
    prompt_version: str
    bundle_sha256: str
    prompt_sha256: str | None
    outcome: Outcome
    attempts: Annotated[int, Field(ge=0)]
    stop_reason: str | None = None
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    latency_ms: Annotated[int, Field(ge=0)] = 0
    # None when no price is configured: an absent figure, never a zero.
    cost_usd: float | None = None
    diagnosis: Diagnosis | None = None
    errors: list[str] = []

    @model_validator(mode="after")
    def _diagnosis_only_when_ok(self) -> DiagnosisRecord:
        if (self.outcome == Outcome.OK) != (self.diagnosis is not None):
            raise ValueError("a diagnosis is kept exactly when the outcome is ok")
        return self