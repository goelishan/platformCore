"""
Signal <-> row mapping. The only place that knows the envelope has a table shape.

  - Kept out of envelope/ so the contract stays storage-agnostic.
  - expires_at is computed here, never supplied by a collector. Retention is a
    landing-zone policy; a source does not decide how long we keep its data.
  - payload JSON is serialised with sorted keys so the bytes are deterministic,
    which is what makes the M4 bundle hash stable across runs.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from oncall import config
from oncall.envelope import Signal, iso

# Columns a collector owns. run_id and expires_at are landing-zone additions and are
# listed separately; incident_id appears in neither, because it is back-filled by us
# and must survive a re-collection untouched.

COLLECTOR_COLUMNS = (
    "fingerprint", "source", "kind", "event_time", "collected_at","cluster",
    "namespace", "subject_kind", "subject_name", "subject_uid",
    "owner_kind", "owner_name", "node_name",
    "severity", "payload", "redacted", "blob_id"
)


LANDING_ZONE_COLUMNS = ("run_id", "expires_at")


def expiry_for(signal: Signal) -> str:
    """Store-side retention, computed here and carried into Postgres with the row.

    Not a buffer lifetime: a signal can be entitled to thirty days in the store and
    still be dropped from the buffer the moment it has shipped. The two tiers keep data
    for different reasons.
    """
    ttl: timedelta = config.STORE_RETENTION.get(
        str(signal.kind), config.STORE_RETENTION_DEFAULT
    )
    return iso(signal.collected_at + ttl)

def to_row(signal: Signal, run_id: str | None) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "fingerprint": signal.fingerprint,
        "source": signal.source,
        "kind": str(signal.kind),
        "event_time": iso(signal.event_time),
        "collected_at": iso(signal.collected_at),
        "cluster": signal.cluster,
        "namespace": signal.namespace,
        "subject_kind": signal.subject.kind if signal.subject else None,
        "subject_name": signal.subject.name if signal.subject else None,
        "subject_uid": signal.subject.uid if signal.subject else None,
        "owner_kind": signal.owner.kind if signal.owner else None,
        "owner_name": signal.owner.name if signal.owner else None,
        "node_name": signal.node,
        "severity": str(signal.severity) if signal.severity else None,
        "payload": json.dumps(signal.payload, sort_keys=True, default=str),
        "redacted": int(signal.redacted),
        "blob_id": signal.blob_id,
        "run_id": run_id,
        "expires_at": expiry_for(signal),
    }