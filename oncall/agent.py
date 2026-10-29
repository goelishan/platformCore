"""
The long-running agent: collection, shipping, retention and triage on their own cadence.

  - One thread, one job at a time. Every job writes the same SQLite buffer, which has
    one writer at a time anyway, and sequential jobs keep a slow API server from
    stacking concurrent collections behind it.
  - Each source keeps its own interval from POLL_INTERVALS. A job that falls behind
    runs once when it is next reached, never several times to catch up: a burst of
    back-to-back collections reads the same cluster state and only costs API calls.
  - A job that raises is logged and rescheduled. A collector raising has escaped its
    own error handling and its run row stays open, which is the record of it.
  - The heartbeat file is touched after every pass of the loop, so the liveness probe
    tests that the scheduler is moving, not that the cluster is healthy. A dead API
    server is reported as unavailable sources; restarting the agent would not fix it.
  - Triage (oncall/triage.py) runs last in each cycle: it reads what the collectors just
    wrote, diagnoses what has been failing long enough, and closes what has gone quiet.
  - SIGTERM stops between jobs. A job in progress finishes, so no run row is left open
    by a deliberate shutdown.

    python -m oncall.agent
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from oncall import config

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval: float
    fn: Callable[[], object]
    # Seconds after start before the first run. Shipping waits: on a fresh install the
    # store starts with the agent and is rarely accepting connections on the first pass.
    start_after: float = 0.0
    next_due: float = 0.0
    runs: int = 0
    failures: int = 0
    last_error: str | None = field(default=None, repr=False)


class Scheduler:
    """Runs whichever jobs are due, in the order they were given."""

    def __init__(self, jobs: Iterable[Job], clock: Callable[[], float] = time.monotonic):
        self.jobs = list(jobs)
        self.clock = clock
        start = clock()
        for job in self.jobs:
            job.next_due = start + job.start_after

    def due_now(self, name: str) -> None:
        """Bring one job forward to the next tick: the webhook opened an incident."""
        for job in self.jobs:
            if job.name == name:
                job.next_due = self.clock()

    def tick(self) -> float:
        """Run every due job once; return the clock value at which the next one is due."""
        for job in self.jobs:
            now = self.clock()
            if now < job.next_due:
                continue
            try:
                job.fn()
                job.last_error = None
            except Exception as exc:  # noqa: BLE001 - one job never stops the others
                job.failures += 1
                job.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("job %s failed", job.name)
            job.runs += 1
            # On cadence when on time; one interval after it actually ran when late, so
            # a stalled job resumes its rhythm instead of firing again straight away.
            job.next_due += job.interval
            if job.next_due <= self.clock():
                job.next_due = self.clock() + job.interval
        return min(job.next_due for job in self.jobs)


def heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()



#--------------------------------------------------------------------------------------------------------
# JOBS
#--------------------------------------------------------------------------------------------------------
#
# Imports are inside each job so the scheduler and its tests never load the Kubernetes
# client or psycopg.



def collect_job(source) -> Callable[[], object]:
    def run() -> object:
        from oncall.collectors import registry

        status, count = registry.run(source)
        log.info("collected %s: %s, %d signals", source, status, count)
        return status

    return run


def ship() -> object:
    from oncall import shipper

    status, shipped = shipper.ship_all()
    log.info("shipped %d rows: %s", shipped, status)
    return status


def sweep_buffer() -> object:
    from oncall import landing_zone as lz

    with lz.connect() as conn:
        removed = lz.sweep_buffer(conn)
        blobs = lz.sweep_blobs(conn)
    with lz.connect() as conn:
        lz.reclaim(conn)
    log.info("buffer sweep: %s; blobs: %s", removed, blobs)
    return removed


def sweep_store() -> object:
    """Runs on the first pass, before the first ship has migrated a fresh store, so it
    migrates first itself. Already migrated, the call is a latch check and costs nothing."""
    from oncall import shipper, store

    shipper.ensure_schema()
    with store.connect() as conn:
        result = store.sweep(conn)
    log.info("store sweep: %s", result)
    return result


def triage() -> object:
    from oncall import triage as t

    report = t.run_once()
    log.info(
        "triage: diagnosed %s; resolved %d; skipped %d%s",
        report.diagnosed or "none", len(report.resolved), len(report.skipped),
        f"; witnesses blind: {', '.join(report.blind)}" if report.blind else "",
    )
    return report


def evaluate() -> object:
    from oncall.eval import online

    done = online.evaluate_pending()
    log.info("evaluated: %s", done)
    return done


def jobs() -> list[Job]:
    """Collectors first, in registry order, which is least recoverable first: events are
    garbage-collected by the API server and lost if missed, so they run before anything
    else on every start."""
    from oncall.collectors import registry

    out = [Job(f"collect:{s}", registry.interval_for(s), collect_job(s))
           for s in registry.COLLECTORS]
    out.append(Job("ship", config.SHIP_INTERVAL, ship, start_after=config.SHIP_INTERVAL / 2))
    out.append(Job("sweep:buffer", config.BUFFER_SWEEP_INTERVAL, sweep_buffer))
    out.append(Job("sweep:store", config.STORE_SWEEP_INTERVAL, sweep_store))
    # Every diagnosis, manual or automatic, gets its checks and later its outcome.
    out.append(Job("evaluate", config.EVALUATE_INTERVAL, evaluate))
    # Last, so a pass reads what this cycle's collectors have just written. Only where a
    # diagnosis can be made at all: on kind there is no AWS identity to make one with.
    if config.TRIAGE_ENABLED and config.DIAGNOSE_ENABLED:
        out.append(Job("triage", config.TRIAGE_INTERVAL, triage,
                       start_after=config.TRIAGE_START_AFTER))
    return out



#--------------------------------------------------------------------------------------------------------
# ENTRY POINT
#--------------------------------------------------------------------------------------------------------



def run(scheduler: Scheduler, stop: threading.Event, beat: Path,
        wake: threading.Event | None = None) -> None:
    """wake, when set by the webhook, ends the wait early and makes triage due now."""
    wake = wake or stop
    while not stop.is_set():
        due = scheduler.tick()
        heartbeat(beat)
        if wake.wait(max(0.0, due - scheduler.clock())) and wake is not stop:
            wake.clear()
            scheduler.due_now("triage")


def main() -> int:
    from oncall import landing_zone as lz

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    lz.bootstrap()

    stop = threading.Event()
    wake = threading.Event()

    def _stop(*_: object) -> None:
        stop.set()
        wake.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)

    scheduler = Scheduler(jobs())
    log.info("agent started for cluster %s: %s", config.CLUSTER_NAME,
             ", ".join(f"{j.name}/{j.interval:g}s" for j in scheduler.jobs))
    server = None
    if config.WEBHOOK_ENABLED:
        from oncall import webhook

        server = webhook.serve(config.WEBHOOK_PORT, on_opened=wake.set)
    run(scheduler, stop, config.HEARTBEAT_PATH, wake)
    if server is not None:
        server.shutdown()
    log.info("agent stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
