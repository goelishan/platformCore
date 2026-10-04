"""
golden.yaml into Case objects, and the fixture paths that go with them.

  - Every expected label and every also_accept must be a member of CauseClass, checked
    when loading. A typo in a label would otherwise score every run of that case as
    wrong and read as a model failure.
  - "cluster" is the one key that means no subject: the whole-cluster bundle.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from oncall.diagnosis.schema import CauseClass
from oncall.eval.metrics import Case

EVAL_DIR = Path(__file__).resolve().parent
CLUSTER_KEY = "cluster"


def subject_of(key: str) -> str | None:
    return None if key == CLUSTER_KEY else key


def load(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text()) or {}
    vocabulary = {c.value for c in CauseClass}
    cases = []
    for subject, entry in raw.items():
        entry = entry or {}
        expected = entry.get("expected")
        also = tuple(entry.get("also_accept") or ())
        unknown = [c for c in (expected, *also) if c is not None and c not in vocabulary]
        if unknown:
            raise ValueError(f"{path}: {subject} uses labels outside CauseClass: {unknown}")
        scored = bool(entry.get("scored", True)) and expected is not None
        cases.append(Case(subject, expected, also, scored, str(entry.get("rationale", ""))))
    return cases
