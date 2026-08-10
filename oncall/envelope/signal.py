"""
Signal envelope — the one shape every collector writes into the landing zone.

  - Uniform across sources so correlation is a single join, not glue per source pair.
  - Carries only what a collector observed. Fields the landing zone owns —
    run_id, incident_id, expires_at — are deliberately absent from the model, so a
    collector cannot set them even by accident.
  - event_time and collected_at stay distinct; sources disagree about clocks.
  - fingerprint identifies a recurring problem, signal_id one occurrence of it.
    Both are content-derived, which makes repeated collection idempotent.
"""


from __future__ import annotations
import hashlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field, field_validator

from oncall.envelope.enums import Severity, SignalKind, SignalSource
from oncall.envelope.timefmt import iso

# ---- subject and owner -----------------------------------------------------
# Subject is what the signal describes. Owner is the workload it rolls up to, and
# is what lets a pod crash join to the Deployment rollout that caused it.


class Subject(BaseModel):
    kind: str
    name: str
    uid: str | None = None

class Owner(BaseModel):
    kind: str
    name: str

# ---- envelope --------------------------------------------------------------

class Signal(BaseModel):
    source: SignalSource
    kind: SignalKind
    cluster: str
    event_time: datetime

    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    namespace: str | None = None
    subject: Subject | None = None
    owner: Owner | None = None

    # Where the subject was running. A third location dimension alongside subject
    # and owner, promoted to a column because "five pods failing across five nodes"
    # and "five pods failing on one node" are different diagnoses, and telling them
    # apart is a GROUP BY. Deliberately absent from the fingerprint: a rescheduled
    # pod is the same problem on a different node, not a different problem.
    node: str | None = None

    severity: Severity | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    redacted: bool = False
    blob_id: str | None = None

    # What makes two occurrences the same problem. Only the collector knows which
    # payload field carries that meaning, so it supplies the key rather than the
    # envelope guessing. Empty means the subject alone identifies the problem,
    # which is almost never true — treat an empty key in a collector as a bug.
    dedupe_key: str = ""

    @field_validator("event_time", "collected_at")
    @classmethod
    def _must_be_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Naive date time rejected")
        return v.astimezone(UTC)

    @computed_field
    @property
    def fingerprint(self) -> str:
        basis = "|".join([
            self.source,
            str(self.kind),
            self.cluster,
            self.namespace or "",
            self.subject.kind if self.subject else "",
            self.subject.name if self.subject else "",
            self.dedupe_key,
        ])
        return hashlib.sha256(basis.encode()).hexdigest()[:32]

    @computed_field
    @property
    def signal_id(self) -> str:
        return hashlib.sha256(
            f"{self.fingerprint}|{iso(self.event_time)}".encode()
        ).hexdigest()[:32]