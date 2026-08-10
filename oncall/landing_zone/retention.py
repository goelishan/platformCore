"""
Retention sweep.

  - Value decays at different rates, so expiry is set per kind at write time: log
    excerpts are bulky and stale within a day, deploy records are tiny and are the
    first thing wanted in any postmortem.
  - Signals attached to an incident are exempt. They are the eval corpus, and deleting
    them destroys the ability to measure whether diagnoses are improving.
  - Blob rows are deleted here; the files on disk are not. Unlinking belongs to the
    blob store in M3, so the orphaned paths are returned for the caller to remove.
"""

from __future__ import annotations

from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Any

from oncall.envelope import iso


# Defined once and used by both the SELECT that finds orphans and the DELETE that
# removes them. Two hand-written copies would eventually drift, and then rows would be
# deleted that were never inspected for their on-disk files.
_ORPHAN_BLOBS = (
    "expires_at < ? AND blob_id NOT IN "
    "(SELECT blob_id FROM signals WHERE blob_id IS NOT NULL)"
)


def sweep_expired(conn: Connection, now: datetime | None = None) -> dict[str, Any]:
    """The now override exists so tests can freeze time rather than wait out a TTL."""
    cutoff = iso(now or datetime.now(UTC))

    # Signals first: deleting them is what orphans the blobs found below. Reversing the
    # order would find nothing to clean.
    deleted_signals = conn.execute(
        "DELETE FROM signals WHERE expires_at < ? AND incident_id IS NULL",
        (cutoff,),
    ).rowcount

    orphan_paths = [
        r["path"]
        for r in conn.execute(
            f"SELECT path FROM blobs WHERE {_ORPHAN_BLOBS}", (cutoff,)
        ).fetchall()
    ]

    deleted_blobs = conn.execute(
        f"DELETE FROM blobs WHERE {_ORPHAN_BLOBS}", (cutoff,)
    ).rowcount

    return {
        "signals": deleted_signals,
        "blobs": deleted_blobs,
        "orphan_paths": orphan_paths,
    }
