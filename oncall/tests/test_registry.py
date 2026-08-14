"""
Registry contract tests.

The bug this file exists for cannot be caught by reading the code: run_once accepts a
collect function and a source as unrelated arguments, so a mismatched pair type-checks,
runs, writes rows and reports ok. The damage lands in collection_runs, where last_run()
and source_status_in_window() then vouch for a source that never executed — the two
queries whose entire purpose is stopping absence from reading as health.
"""

from __future__ import annotations

import pytest

from oncall import config
from oncall import landing_zone as lz
from oncall.collectors import k8s_events, k8s_pods, registry
from oncall.envelope import SignalSource, SourceStatus

# The buffer fixture lives in conftest.py. One definition, so a change to how storage
# is redirected cannot be applied to some test files and forgotten in others.


# ---- the binding -----------------------------------------------------------


def test_each_source_maps_to_its_own_collector():
    assert registry.COLLECTORS[SignalSource.K8S_EVENTS] is k8s_events.collect
    assert registry.COLLECTORS[SignalSource.K8S_PODS] is k8s_pods.collect


def test_events_are_collected_before_pod_state():
    """Registry order is collection order, least recoverable first. Events are dropped
    by the API server once they stop being updated; pod state can be re-read at any
    time and loses only freshness by going second."""
    order = list(registry.COLLECTORS)
    assert order.index(SignalSource.K8S_EVENTS) < order.index(SignalSource.K8S_PODS)


def test_logs_are_collected_last_despite_being_unrecoverable():
    """Logs break the least-recoverable-first rule, and the exception is deliberate.
    The collector is signal-driven, so its targets are rows the other two sources have
    just written; running earlier would find the previous cycle's triggers or none at
    all, and the dependency beats the ordering principle. The cost is real — a previous
    container's log dies at its next restart — and is paid down by LOG_LOOKBACK_SECONDS
    and LOG_SINCE_SECONDS each spanning several intervals."""
    assert list(registry.COLLECTORS)[-1] == SignalSource.K8S_LOGS


def test_unregistered_source_names_what_is_registered():
    with pytest.raises(KeyError, match="prometheus"):
        registry.run(SignalSource.PROMETHEUS)


# ---- cadence consistency ---------------------------------------------------


def test_every_collector_has_an_interval():
    """Import-time validation already asserts this; pinning it separately means the
    failure names the missing cadence instead of surfacing as an import error in an
    unrelated test."""
    for source in registry.COLLECTORS:
        assert registry.interval_for(source) > 0


def test_validation_rejects_a_collector_with_no_cadence(monkeypatch):
    """The silent half of the drift: it would simply never be scheduled."""
    monkeypatch.setitem(registry.COLLECTORS, SignalSource.PROMETHEUS, lambda c: None)

    with pytest.raises(RuntimeError, match="prometheus"):
        registry._validate()


def test_validation_rejects_an_interval_with_no_collector(monkeypatch):
    """The other half: a typo that reads as a configured source forever."""
    monkeypatch.setitem(config.POLL_INTERVALS, "k8s_podz", 60)

    with pytest.raises(RuntimeError, match="k8s_podz"):
        registry._validate()


# ---- isolation between sources ---------------------------------------------


def test_a_raising_collector_does_not_stop_the_others(buffer, monkeypatch):
    """A collector that raises rather than returning unavailable has escaped its own
    error handling, so the fault is in the mapping, not the cluster. The remaining
    sources still have to run: one source's evidence plus a visibly incomplete run
    beats no evidence at all."""

    def boom(cluster):
        raise RuntimeError("mapping blew up")

    def empty(cluster):
        return SourceStatus.EMPTY, [], None

    monkeypatch.setattr(
        registry,
        "COLLECTORS",
        {SignalSource.K8S_EVENTS: boom, SignalSource.K8S_PODS: empty},
    )

    results = registry.run_all()

    assert results[SignalSource.K8S_EVENTS] is None
    assert results[SignalSource.K8S_PODS] == (SourceStatus.EMPTY, 0)


def test_a_raising_collector_leaves_its_run_open(buffer, monkeypatch):
    """No status is invented for the crash. The run row was committed before collection
    began and still has no finished_at, which is a third state — distinct from both
    'finished and found nothing' and 'finished, source was down'."""

    def boom(cluster):
        raise RuntimeError("mapping blew up")

    monkeypatch.setattr(registry, "COLLECTORS", {SignalSource.K8S_EVENTS: boom})
    registry.run_all()

    with lz.connect() as conn:
        row = lz.last_run(conn, SignalSource.K8S_EVENTS)

    assert row is not None
    assert row["started_at"] is not None
    assert row["finished_at"] is None


def test_run_writes_under_the_source_it_was_asked_for(buffer, monkeypatch):
    """The whole point of the registry. Nothing can hand run_once a function belonging
    to a different source, because nothing hands it a function at all."""
    monkeypatch.setattr(
        registry,
        "COLLECTORS",
        {SignalSource.K8S_EVENTS: lambda c: (SourceStatus.EMPTY, [], None)},
    )

    registry.run(SignalSource.K8S_EVENTS)

    with lz.connect() as conn:
        assert lz.last_run(conn, SignalSource.K8S_EVENTS) is not None
        assert lz.last_run(conn, SignalSource.K8S_PODS) is None
