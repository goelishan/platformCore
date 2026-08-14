"""
The one place a source, its collector and its cadence are bound together.

  - run_once takes the collect function and the source as independent arguments.
    That independence is what makes it generic, and also what lets the two disagree:
    pairing them by hand writes one source's signals under another source's name and
    nothing errors — rows land, the run reports ok, and last_run() then vouches for a
    source that never ran. Those staleness queries exist so absence cannot be read as
    health, which makes this the worst place in the system for a silent mistake.
    Looking the function up by source removes the opportunity entirely.
  - Deliberately not in __init__.py. Importing a package should not drag in every
    client library its submodules happen to need, and the registry imports all of them.
  - Consistency is checked at import rather than at first use. A collector with no
    cadence is never scheduled and nothing downstream notices its absence, so this is
    exactly the class of mistake that has to fail loudly and early.
"""

from __future__ import annotations

import logging

from oncall import config
from oncall.collectors import k8s_events, k8s_logs, k8s_pods
from oncall.collectors.runner import CollectFn, run_once
from oncall.envelope import SignalSource, SourceStatus

log = logging.getLogger(__name__)


# Insertion order is collection order, and it runs from least recoverable to most.
# Events are dropped by the API server once they stop being updated, so a cycle cut
# short after the first source has to have collected them already. Pod state is a
# snapshot that can always be re-read, and loses only freshness by going second.
#
# Logs break that ordering and go last anyway, because they are signal-driven: their
# targets are rows the first two sources just wrote, so running earlier would find the
# previous cycle's triggers or none at all. The dependency beats recoverability here,
# and the cost is real — a previous container's log dies at its next restart, so a
# cycle cut short before this point loses evidence that cannot be re-read. It is paid
# down by LOG_LOOKBACK_SECONDS and LOG_SINCE_SECONDS each spanning several intervals,
# so a trigger from the previous cycle is still in the window and its logs still in
# range.
COLLECTORS: dict[SignalSource, CollectFn] = {
    SignalSource.K8S_EVENTS: k8s_events.collect,
    SignalSource.K8S_PODS: k8s_pods.collect,
    SignalSource.K8S_LOGS: k8s_logs.collect,
}


def _validate() -> None:
    """Registered collectors and configured intervals must describe the same set.

    A SignalSource member with no entry here is fine — k8s_logs, prometheus and argocd
    are declared before they are built. What is not fine is the two halves drifting:
    a collector with no interval never runs, and an interval naming no collector is a
    typo that will read as a configured source forever.
    """
    uncadenced = {str(s) for s in COLLECTORS if s not in config.POLL_INTERVALS}
    unclaimed = {str(k) for k in config.POLL_INTERVALS if k not in COLLECTORS}

    if uncadenced or unclaimed:
        raise RuntimeError(
            "collector registry and POLL_INTERVALS disagree; "
            f"no interval for {sorted(uncadenced)}, no collector for {sorted(unclaimed)}"
        )


_validate()


# ---- running ---------------------------------------------------------------


def interval_for(source: SignalSource) -> int:
    return config.POLL_INTERVALS[source]


def run(source: SignalSource) -> tuple[SourceStatus, int]:
    """One cycle for one source. The only entry point production code should use —
    run_once stays public so tests can drive a stub collector through the same path."""
    try:
        collect_fn = COLLECTORS[source]
    except KeyError:
        raise KeyError(
            f"no collector registered for {source}; registered: "
            f"{sorted(str(s) for s in COLLECTORS)}"
        ) from None

    return run_once(collect_fn, source)


def run_all() -> dict[SignalSource, tuple[SourceStatus, int] | None]:
    """Every source, in registry order, isolated from one another.

    A collector that raises rather than returning unavailable has escaped its own
    error handling, which means the failure is in the mapping and not in the cluster.
    That must not stop the remaining sources: an assembler is far better served by
    one source's evidence plus a visibly incomplete run than by nothing at all.

    None marks a source whose collector raised. No status is invented for it, because
    the record already exists and is more accurate than anything returned here — the
    run row was committed before collection began and still has no finished_at, which
    is the third state run_once was written to produce.
    """
    results: dict[SignalSource, tuple[SourceStatus, int] | None] = {}

    for source, collect_fn in COLLECTORS.items():
        try:
            results[source] = run_once(collect_fn, source)
        except Exception:
            log.exception("collector for %s raised; leaving its run open", source)
            results[source] = None

    return results
