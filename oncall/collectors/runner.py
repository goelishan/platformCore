"""
One collection cycle: open a run, collect, write, close the run.

  - Generic over collectors. Anything returning (status, signals, error) works here
    unchanged, so k8s_events and prometheus reuse it without modification.
  - Collection happens outside any transaction. Holding a SQLite write lock across
    network calls would block the single writer for as long as the API server is
    slow, and buys nothing: the snapshot only has to be atomic when it lands.
  - start_run commits before collection begins. A process killed mid-collection then
    leaves a run with started_at and no finished_at — a third state, distinct from
    both "finished and found nothing" and "finished, source was down".
"""

from __future__ import annotations

from collections.abc import Callable

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope import Signal, SignalSource, SourceStatus

CollectFn = Callable[[str], tuple[SourceStatus, list[Signal], str | None]]


def run_once(
    collect_fn: CollectFn,
    source: SignalSource,
    cluster: str | None = None,
) -> tuple[SourceStatus, int]:
    cluster = cluster or config.CLUSTER_NAME

    with lz.connect() as conn:
        run_id = lz.start_run(conn, source, cluster)

    status, signals, error = collect_fn(cluster)

    with lz.connect() as conn:
        written = lz.write_signals(conn, run_id, signals)
        lz.finish_run(conn, run_id, status, written, error)

    return status, written
