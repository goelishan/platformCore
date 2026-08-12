"""
Buffer row -> store row.

  - The shipper sends whatever the buffer currently holds, which is a database row, not
    a Signal. Converting from the row rather than re-deriving from a Signal is what
    lets landing-zone-only fields — incident_id above all — survive the trip.
  - Every conversion here is text to a real type. The buffer stores timestamps and JSON
    as text because SQLite has no better option; the store has both, and using them is
    half the reason for the store existing.
  - Nothing is recomputed. signal_id and fingerprint arrive as written and are copied
    verbatim, so a change to how identity is derived can never silently apply to some
    rows and not others.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb

from oncall.envelope import parse

# Order matters only in that it must match between the insert list and the conflict
# update list; both are generated from this tuple for exactly that reason.
SIGNAL_COLUMNS = (
    "signal_id",
    "fingerprint",
    "source",
    "kind",
    "event_time",
    "collected_at",
    "cluster",
    "namespace",
    "subject_kind",
    "subject_name",
    "subject_uid",
    "owner_kind",
    "owner_name",
    "node_name",
    "severity",
    "payload",
    "redacted",
    "blob_id",
    "run_id",
    "incident_id",
    "expires_at",
)

_TIMESTAMPS = ("event_time", "collected_at", "expires_at")

INCIDENT_COLUMNS = (
    "incident_id",
    "opened_at",
    "closed_at",
    "cluster",
    "namespace",
    "trigger",
    "title",
    "state",
)

DIAGNOSIS_COLUMNS = (
    "diagnosis_id",
    "incident_id",
    "created_at",
    "model",
    "bundle_sha256",
    "hypotheses",
    "commands",
    "input_tokens",
    "output_tokens",
    "latency_ms",
)


def _ts(value: str | None):
    return parse(value) if value else None


def _json(value: str | None):
    """Text to jsonb. Jsonb() rather than passing the string through, so psycopg adapts
    it as a value instead of the store receiving a JSON document that happens to be a
    quoted string."""
    if value is None:
        return None
    return Jsonb(json.loads(value) if isinstance(value, str) else value)


def signal_from_buffer(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {c: row[c] for c in SIGNAL_COLUMNS}

    for column in _TIMESTAMPS:
        out[column] = _ts(out[column])

    out["payload"] = _json(out["payload"])
    # SQLite has no boolean; the buffer stores 0 or 1 and the store wants the real type.
    out["redacted"] = bool(out["redacted"])
    return out


def incident_from_buffer(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {c: row[c] for c in INCIDENT_COLUMNS}
    out["opened_at"] = _ts(out["opened_at"])
    out["closed_at"] = _ts(out["closed_at"])
    return out


def diagnosis_from_buffer(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {c: row[c] for c in DIAGNOSIS_COLUMNS}
    out["created_at"] = _ts(out["created_at"])
    out["hypotheses"] = _json(out["hypotheses"])
    out["commands"] = _json(out["commands"])
    return out
