"""
The version stamp on a stored row.

The column exists because changing the mask rules re-keys some population of messages
with nothing raising, so the tests here are about who is allowed to set it and when —
not about the value, which is a constant.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oncall.envelope import (
    NORMALIZER_VERSION,
    Signal,
    SignalKind,
    SignalSource,
    Subject,
)
from oncall.landing_zone.rows import (
    COLLECTOR_COLUMNS,
    LANDING_ZONE_COLUMNS,
    to_row,
)


def _signal(**overrides) -> Signal:
    base = {
        "source": SignalSource.K8S_PODS,
        "kind": SignalKind.POD_STATE,
        "cluster": "oncall-dev",
        "event_time": datetime(2026, 8, 14, 3, 14, 7, tzinfo=UTC),
        "namespace": "oncall-lab",
        "subject": Subject(kind="Pod", name="web-1", uid="u-1"),
        "dedupe_key": "app|CrashLoopBackOff|1",
    }
    return Signal(**{**base, **overrides})


def test_every_row_carries_the_running_version():
    assert to_row(_signal(), run_id="r-1")["normalizer_version"] == NORMALIZER_VERSION


def test_the_envelope_cannot_set_it():
    """It describes the process doing the writing, not anything a collector observed.
    A field on Signal would be a field a collector could get wrong, and a row claiming
    the wrong version is worse than a row claiming none — it would be trusted."""
    assert not hasattr(_signal(), "normalizer_version")


def test_it_is_a_landing_zone_column_not_a_collector_one():
    """Placement decides whether the upsert updates it. In COLLECTOR_COLUMNS it would
    be indistinguishable from observed data; outside both tuples it would be inserted
    once and then frozen while every other column kept moving."""
    assert "normalizer_version" in LANDING_ZONE_COLUMNS
    assert "normalizer_version" not in COLLECTOR_COLUMNS


def test_re_collection_restamps_the_row():
    """A re-collected signal has had its key recomputed by the running normaliser, so
    the running version is the only value that is true of it."""
    signal = _signal()
    first = to_row(signal, run_id="r-1")
    again = to_row(signal, run_id="r-2")

    assert first["signal_id"] == again["signal_id"]
    assert again["normalizer_version"] == NORMALIZER_VERSION
