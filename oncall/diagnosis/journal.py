"""
A DiagnosisRecord into the buffer's diagnoses table.

  - Through landing_zone.record_diagnosis, the one write path, so the row and its outbox
    entry land in one transaction and reach the store the way every other row does. The
    store is most likely to be unreachable during an incident, which is exactly when
    diagnoses are written.
  - hypotheses keeps each hypothesis whole, citations as fingerprints. commands repeats
    the verify commands flat, with the rank they belong to, because "which commands did
    the model suggest" is asked across diagnoses and should not need JSON unnesting.
  - unknowns is kept as the model wrote it. It is the diagnosis's own account of what the
    evidence could not show, and without it a later reader sees a confident answer with
    its stated blind spots removed.
  - The landing zone stays ignorant of the diagnosis package: it takes plain values,
    and this module is the only place that knows both shapes.
"""

from __future__ import annotations

from sqlite3 import Connection

from oncall.diagnosis.schema import DiagnosisRecord
from oncall.landing_zone import record_diagnosis


def journal(conn: Connection, record: DiagnosisRecord) -> str:
    d = record.diagnosis
    hypotheses = [h.model_dump(mode="json") for h in d.hypotheses] if d else []
    commands = [
        {"rank": h.rank, "command": c.command, "purpose": c.purpose}
        for h in (d.hypotheses if d else [])
        for c in h.verify
    ]
    return record_diagnosis(
        conn,
        record.incident_id,
        model=record.model,
        bundle_sha256=record.bundle_sha256,
        hypotheses=hypotheses,
        commands=commands,
        prompt_sha256=record.prompt_sha256,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        latency_ms=record.latency_ms,
        diagnosis_id=record.diagnosis_id,
        created_at=record.created_at,
        prompt_version=record.prompt_version,
        outcome=str(record.outcome),
        attempts=record.attempts,
        stop_reason=record.stop_reason,
        cost_usd=record.cost_usd,
        summary=d.summary if d else None,
        errors=record.errors,
        insufficient_evidence=d.insufficient_evidence if d else None,
        unknowns=d.unknowns if d else None,
    )
