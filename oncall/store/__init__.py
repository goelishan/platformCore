"""
Public surface of the durable store. Nothing outside this package issues SQL against it.

  - Mirrors the landing zone's convention rather than the collectors': this is a
    contract other packages cross, so a curated surface keeps the internals free to
    move — including a move off Postgres, which the buffer already survived once.
  - Importing this module must not open a connection. Collectors import the package
    tree transitively and have to keep working when the store is down.
  - The division of labour is by question, not by table. The buffer answers "what is
    happening now" and is read on the critical path of a diagnosis. This answers "has
    it happened before, is it getting worse, is it happening elsewhere" — history,
    which is exactly what a two-day buffer cannot hold.
"""

from oncall.store.connection import close, connect, fetch_all, fetch_one, reachable
from oncall.store.migrate import migrate, pending
from oncall.store.reader import (
    clusters_affected,
    diagnoses_for_incident,
    first_seen_ever,
    is_new,
    recurrence_history,
    signals_by_payload,
    signals_for_incident,
)
from oncall.store.retention import drop_expired_partitions, signal_partitions, sweep
from oncall.store.writer import (
    ensure_partitions,
    upsert_diagnoses,
    upsert_incidents,
    upsert_signals,
)

__all__ = [
    "close",
    "clusters_affected",
    "connect",
    "diagnoses_for_incident",
    "drop_expired_partitions",
    "ensure_partitions",
    "fetch_all",
    "fetch_one",
    "first_seen_ever",
    "is_new",
    "migrate",
    "pending",
    "reachable",
    "recurrence_history",
    "signal_partitions",
    "signals_by_payload",
    "signals_for_incident",
    "sweep",
    "upsert_diagnoses",
    "upsert_incidents",
    "upsert_signals",
]
