"""
One bundle in, one journal record out, whatever the model does.

  - diagnose() never raises. A Bedrock error, a malformed answer, a bad citation, an
    unsafe command and evidence over the prompt cap each end in a DiagnosisRecord with
    an outcome and the errors that produced it, because the journal measures failure
    rates and cannot measure what it never received.
  - A malformed answer, a citation to a finding that is not in the bundle, a link in the
    prose, a command aimed at a namespace the evidence never names, or a leading hypothesis that rests only on namespace neighbours or on blind
    sources earns one repair attempt, with the specific errors sent back as the tool
    result. All are mistakes the model can correct from that feedback.
  - Remedies come from a second call, after the diagnosis has passed every check, with
    the hypotheses handed over as data (prompt v9). A step off its allowlist, below its
    risk floor, missing an undo, carrying a credential or aimed outside the evidence's
    namespaces earns that call one repair. If it still fails, the diagnosis stands with
    no steps and the reason in errors: a fix is worth less than the diagnosis it serves.
  - An unsafe verify command earns none. A model that proposes a write to the cluster has
    told us something about its reading of the incident, and asking it to try again only
    teaches it to hide that. The diagnosis is dropped and the record kept.
  - max_tokens earns none either: the same budget truncates the same answer again.
  - Citations are labels in the prompt and fingerprints in the record, so a journalled
    diagnosis means the same thing however the findings were ordered when it was made.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from oncall import config
from oncall.diagnosis import bedrock, guards
from oncall.diagnosis import prompt as prompts
from oncall.diagnosis.schema import Diagnosis, DiagnosisRecord, Outcome, Remedies
from oncall.evidence.bundle import Bundle, evidence_receipt

MAX_ATTEMPTS = 2


def cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    """From the configured list prices; None when either price is unset."""
    if config.PRICE_INPUT_PER_MTOK is None or config.PRICE_OUTPUT_PER_MTOK is None:
        return None
    return round(
        input_tokens / 1e6 * config.PRICE_INPUT_PER_MTOK
        + output_tokens / 1e6 * config.PRICE_OUTPUT_PER_MTOK,
        6,
    )


def _validation_problems(error: ValidationError) -> list[str]:
    """One short line per failed rule, in a form the model can act on."""
    out = []
    for e in error.errors():
        where = ".".join(str(p) for p in e["loc"]) or "diagnosis"
        out.append(f"{where}: {e['msg']}")
    return out


def _to_fingerprints(diagnosis: Diagnosis, labels: dict[str, str]) -> Diagnosis:
    data = diagnosis.model_dump(mode="json")
    for h in data["hypotheses"]:
        h["supporting"] = [labels[c] for c in h["supporting"]]
        h["contradicting"] = [labels[c] for c in h["contradicting"]]
    return Diagnosis.model_validate(data)


# ---- remedies, a second call -------------------------------------------------------
# Made only once the diagnosis has passed every check, with the ranking handed over as
# data. In one call, asking for fixes moved which cause led (prompt v9 notes in prompt.py).
# A remedy failure never costs the diagnosis: the engineer gets the hypotheses with no
# steps and the reason in errors, which the journal and the checks evaluator both see.

REMEDY_ERROR = "remedies"


@dataclass
class Fixes:
    by_rank: dict[int, list[Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


def _merged(diagnosis: Diagnosis, by_rank: dict[int, list[Any]]) -> Diagnosis:
    data = diagnosis.model_dump(mode="json")
    for h in data["hypotheses"]:
        h["remediate"] = [s.model_dump(mode="json") for s in by_rank.get(h["rank"], [])]
    return Diagnosis.model_validate(data)


def _remediate(bundle: Bundle, p: prompts.Prompt, diagnosis: Diagnosis, client: Any,
               model: str) -> Fixes:
    fixes = Fixes()
    if not diagnosis.hypotheses:
        return fixes
    rp = prompts.build_remedy(p, diagnosis)
    messages = bedrock.first_message(rp)
    ranks = {h.rank for h in diagnosis.hypotheses}
    namespaces = evidence_namespaces(bundle)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            reply = bedrock.converse(rp, messages, client=client, model_id=model or None)
        except bedrock.ModelError as e:
            fixes.errors.append(f"{REMEDY_ERROR} attempt {attempt}: {e}")
            return Fixes(errors=fixes.errors, input_tokens=fixes.input_tokens,
                         output_tokens=fixes.output_tokens, latency_ms=fixes.latency_ms)
        fixes.input_tokens += reply.input_tokens
        fixes.output_tokens += reply.output_tokens
        fixes.latency_ms += reply.latency_ms
        if reply.tool_input is None:
            fixes.errors.append(f"{REMEDY_ERROR} attempt {attempt}: no {prompts.REMEDY_TOOL_NAME}"
                                f" call (stop_reason {reply.stop_reason})")
            break
        try:
            parsed = Remedies.model_validate(reply.tool_input)
        except ValidationError as e:
            problems = _validation_problems(e)
        else:
            by_rank = {r.rank: list(r.remediate) for r in parsed.remedies if r.rank in ranks}
            merged = _merged(diagnosis, by_rank)
            problems = guards.check_remedies(merged)
            problems += guards.check_remedy_namespaces(merged, namespaces)
            problems += [q for q in guards.check_prose(merged) if "remediate" in q]
            if not problems:
                fixes.by_rank = by_rank
                return fixes
        fixes.errors += [f"{REMEDY_ERROR} attempt {attempt}: {q}" for q in problems]
        if reply.stop_reason == "max_tokens":
            break
        messages = messages + _repair_turn(reply, problems, prompts.REMEDY_TOOL_NAME)
    # Nothing usable: by_rank was never set, so the diagnosis stands without steps and
    # errors say why.
    return fixes


def evidence_namespaces(bundle: Bundle) -> set[str]:
    """Every namespace the model was shown: the subject's and each finding's."""
    found = {f.namespace for f in bundle.findings if f.namespace}
    if bundle.scope and bundle.scope.namespace:
        found.add(bundle.scope.namespace)
    return found


def _repair_turn(reply: bedrock.Reply, problems: list[str],
                 tool: str = prompts.TOOL_NAME) -> list[dict[str, Any]]:
    what = "diagnosis" if tool == prompts.TOOL_NAME else "set of remedies"
    feedback = (
        f"The {tool} input was rejected:\n"
        + "\n".join(f"- {p}" for p in problems)
        + f"\nCall {tool} again with a corrected {what}."
    )
    return [
        reply.message,
        {
            "role": "user",
            "content": [{"toolResult": {
                "toolUseId": reply.tool_use_id,
                "status": "error",
                "content": [{"text": feedback}],
            }}],
        },
    ]


def diagnose(
    bundle: Bundle,
    *,
    incident_id: str,
    client: Any = None,
    model_id: str | None = None,
) -> DiagnosisRecord:
    model = model_id or config.BEDROCK_MODEL_ID or ""
    try:
        p = prompts.build(bundle)
    except prompts.PromptTooLarge as e:
        return DiagnosisRecord(
            diagnosis_id=uuid.uuid4().hex,
            incident_id=incident_id,
            created_at=datetime.now(UTC),
            model=model,
            prompt_version=prompts.PROMPT_VERSION,
            bundle_sha256=evidence_receipt(bundle),
            prompt_sha256=None,
            outcome=Outcome.PROMPT_TOO_LARGE,
            attempts=0,
            errors=[str(e)],
        )
    by_fingerprint = {f.fingerprint: f for f in bundle.findings}
    by_label = {label: by_fingerprint[fp] for label, fp in p.labels.items()}
    messages = bedrock.first_message(p)

    attempts = input_tokens = output_tokens = latency_ms = 0
    stop_reason: str | None = None
    errors: list[str] = []
    outcome = Outcome.INVALID_OUTPUT
    kept: Diagnosis | None = None

    while attempts < MAX_ATTEMPTS:
        attempts += 1
        try:
            reply = bedrock.converse(p, messages, client=client, model_id=model or None)
        except bedrock.ModelError as e:
            errors.append(f"attempt {attempts}: {e}")
            outcome = Outcome.MODEL_ERROR
            break

        input_tokens += reply.input_tokens
        output_tokens += reply.output_tokens
        latency_ms += reply.latency_ms
        stop_reason = reply.stop_reason

        if reply.tool_input is None:
            errors.append(
                f"attempt {attempts}: no {prompts.TOOL_NAME} call (stop_reason {stop_reason})"
            )
            break

        try:
            diagnosis = Diagnosis.model_validate(reply.tool_input)
        except ValidationError as e:
            problems = _validation_problems(e)
        else:
            unsafe = guards.check_commands(diagnosis)
            if unsafe:
                errors += [f"attempt {attempts}: {u}" for u in unsafe]
                outcome = Outcome.GUARD_REJECTED
                break
            unknown = guards.check_citations(diagnosis, p.labels)
            problems = [f"{c} is not a finding ID in this bundle" for c in unknown]
            problems += guards.check_prose(diagnosis)
            problems += guards.check_grounding(diagnosis, by_label)
            problems += guards.check_namespaces(diagnosis, evidence_namespaces(bundle))
            if not problems:
                outcome = Outcome.OK
                break

        errors += [f"attempt {attempts}: {q}" for q in problems]
        if stop_reason == "max_tokens":
            break
        messages = messages + _repair_turn(reply, problems)

    if outcome == Outcome.OK:
        fixes = _remediate(bundle, p, diagnosis, client, model)
        input_tokens += fixes.input_tokens
        output_tokens += fixes.output_tokens
        latency_ms += fixes.latency_ms
        errors += fixes.errors
        kept = _to_fingerprints(_merged(diagnosis, fixes.by_rank), p.labels)

    return DiagnosisRecord(
        diagnosis_id=uuid.uuid4().hex,
        incident_id=incident_id,
        created_at=datetime.now(UTC),
        model=model,
        prompt_version=p.version,
        bundle_sha256=p.bundle_sha256,
        prompt_sha256=p.prompt_sha256,
        outcome=outcome,
        attempts=attempts,
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd(input_tokens, output_tokens),
        diagnosis=kept,
        errors=errors,
    )
