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

# The bounds the system prompt states. Constants rather than literals in Field() because
# a limit the model is never told costs a repair whenever an answer runs past it.
#
# Raised on 2026-10-12 from 10 and 600. In the prompt v3 eval, 3 of the 5 first-attempt
# failures were these two bounds and nothing else: a CronJob bundle holds one finding per
# Job it spawned, so 11 citations of one fact was a correct answer, and a cluster-wide
# subject needed more than 600 characters to name what it saw. A repair re-sends the whole
# prompt, so each one roughly doubled that diagnosis's cost and latency.
MAX_CITATIONS = 15
MAX_SUMMARY_CHARS = 900

Text = Annotated[str, Field(min_length=1, max_length=2000)]
CitationId = Annotated[str, Field(min_length=1, max_length=64)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Command(_Strict):
    command: Annotated[str, Field(min_length=1, max_length=300)]
    purpose: Annotated[str, Field(min_length=1, max_length=300)]


class RemedyStep(_Strict):
    """One thing the engineer can do to fix the cause, once it is confirmed.

    Never run by the agent, which has no write path to the cluster. The command is
    optional because the right fix is often outside kubectl's reach (a change in git
    that Argo CD will sync, a registry credential, a code fix), and a command invented
    to fill the field would be worse than an honest sentence.
    """

    action: Annotated[str, Field(min_length=1, max_length=300)]
    command: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    risk: Literal["low", "medium", "high"]
    undo: Annotated[str, Field(min_length=1, max_length=300)] | None = None


class Hypothesis(_Strict):
    rank: Annotated[int, Field(ge=1, le=3)]
    cause_class: CauseClass
    title: Annotated[str, Field(min_length=1, max_length=120)]
    mechanism: Text
    confidence: Literal["low", "medium", "high"]
    supporting: Annotated[list[CitationId], Field(min_length=1, max_length=MAX_CITATIONS)]
    contradicting: Annotated[list[CitationId], Field(max_length=MAX_CITATIONS)] = []
    verify: Annotated[list[Command], Field(min_length=1, max_length=3)]
    # Required in the tool schema, defaulted here: journal rows from before prompt v6
    # carry no remedies and must still read back. See tool_input_schema().
    remediate: Annotated[list[RemedyStep], Field(max_length=3)] = []

    @model_validator(mode="after")
    def _one_side_per_citation(self) -> Hypothesis:
        both = set(self.supporting) & set(self.contradicting)
        if both:
            raise ValueError(f"cited as both supporting and contradicting: {sorted(both)}")
        return self


class Diagnosis(_Strict):
    summary: Annotated[str, Field(min_length=1, max_length=MAX_SUMMARY_CHARS)]
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
    """The record_diagnosis tool's inputSchema: shape only, see the module docstring.

    remediate is marked required here although the model defaults it: a key the model
    may omit is a key it will omit, and the default exists only for old journal rows."""
    schema = Diagnosis.model_json_schema()
    hypothesis = schema["$defs"]["Hypothesis"]
    hypothesis["required"] = sorted({*hypothesis.get("required", []), "remediate"})
    return schema


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
    # manual or auto. Set by diagnosis.service, which knows who asked; the reasoner
    # does not, and an eval run is neither.
    requested_by: str | None = None

    @model_validator(mode="after")
    def _diagnosis_only_when_ok(self) -> DiagnosisRecord:
        if (self.outcome == Outcome.OK) != (self.diagnosis is not None):
            raise ValueError("a diagnosis is kept exactly when the outcome is ok")
        return self
