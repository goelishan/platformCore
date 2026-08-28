"""
Where a payload key goes when it is machinery rather than evidence.

  - _provenance holds what the assembler may read and the reasoner never sees:
    pointers into other rows, the identity of whatever did the counting, the interval
    a fetch asked for. Load-bearing, and not findings.
  - The collector names its own. Only the collector knows what its fields mean, which
    is the same argument that makes dedupe_key collector-supplied rather than guessed
    by the envelope. A central list here would go stale the first time a collector is
    written by someone who never opens this file.
  - Hidden, not dropped. Every one of these keys is read by something: event_uid is
    how a restarted counter is detected, trigger_* is how an excerpt joins to the
    failure it explains, and window_start is what makes an excerpt comparable to
    anything else. A consumer that only filtered them out of its output would have to
    keep a second list saying which ones it still needs.
  - Absent on rows written before this existed. A consumer finding no _provenance
    treats every key as evidence, which is exactly how those rows already read.
"""

from __future__ import annotations

from typing import Any, Iterable

PROVENANCE_KEY = "_provenance"


def partition_payload(payload: dict[str, Any], provenance: Iterable[str]) -> dict[str, Any]:
    """Drop unset keys, then move the named ones under _provenance.

    None-dropping happens first so that a provenance key which was never set does not
    create an empty entry, and so the whole shape is built in one place rather than in
    three collectors that each have to remember both halves.
    """
    kept = {k: v for k, v in payload.items() if v is not None}
    hidden = {k: kept.pop(k) for k in provenance if k in kept}

    if hidden:
        kept[PROVENANCE_KEY] = hidden

    return kept


def visible(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != PROVENANCE_KEY}


def provenance_of(payload: dict[str, Any]) -> dict[str, Any]:
    found = payload.get(PROVENANCE_KEY)
    return found if isinstance(found, dict) else {}
