"""
Severity basis tests: two scales under one column name, kept apart.

  - k8s_events copies Kubernetes' Normal/Warning into severity and marks the row
    severity_basis="cluster". Every other source assesses on four levels. The corpus
    audit found a Normal NodeNotReady event reading info beside a k8s_nodes row
    calling the same node critical.
  - The basis is a tie-break, never a demotion. These tests pin both halves: a
    judgement leads a copied label at the same level, and a higher level still wins
    whatever its basis.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncall import landing_zone as lz
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    partition_payload,
)
from oncall.evidence import bundle

CLUSTER = "test-cluster"
POD = "api-7f9"

NOW = datetime.now(UTC).replace(microsecond=0)
START = NOW - timedelta(minutes=30)
END = NOW + timedelta(minutes=1)


def pod_state(severity: Severity, minutes_ago: int, key: str = "pod") -> Signal:
    return Signal(
        source=SignalSource.K8S_PODS,
        kind=SignalKind.POD_STATE,
        cluster=CLUSTER,
        event_time=NOW - timedelta(minutes=minutes_ago),
        namespace="prod",
        subject=Subject(kind="Pod", name=POD, uid="uid-1"),
        severity=severity,
        dedupe_key=key,
        payload={"reason": "CrashLoopBackOff"},
    )


def event(severity: Severity, minutes_ago: int, key: str = "event") -> Signal:
    """An event as k8s_events writes it: the basis stamped as provenance."""
    return Signal(
        source=SignalSource.K8S_EVENTS,
        kind=SignalKind.EVENT,
        cluster=CLUSTER,
        event_time=NOW - timedelta(minutes=minutes_ago),
        namespace="prod",
        subject=Subject(kind="Pod", name=POD, uid="uid-1"),
        severity=severity,
        dedupe_key=key,
        payload=partition_payload(
            {"reason": "BackOff", "severity_basis": "cluster"},
            frozenset({"severity_basis"}),
        ),
    )


def store(*signals: Signal) -> None:
    by_source: dict[SignalSource, list[Signal]] = {}
    for s in signals:
        by_source.setdefault(s.source, []).append(s)
    with lz.connect() as conn:
        for source, batch in by_source.items():
            run = lz.start_run(conn, source, CLUSTER)
            # Stamped after the run starts, as every real collector's signals are:
            # the bundle reads collected_at against started_at to judge state.
            now = datetime.now(UTC)
            batch = [s.model_copy(update={"collected_at": now}) for s in batch]
            lz.write_signals(conn, run, batch)
            lz.finish_run(conn, run, SourceStatus.OK, len(batch))


def build() -> bundle.Bundle:
    with lz.connect() as conn:
        return bundle.build(conn, CLUSTER, START, END, subject_name=POD, history=False)


# ---- reading the basis ------------------------------------------------------


def test_an_event_carries_the_cluster_basis_from_its_provenance(buffer):
    store(event(Severity.WARNING, 5))

    (finding,) = build().findings

    assert finding.severity_basis == bundle.BASIS_CLUSTER


def test_a_row_with_no_basis_is_an_assessment(buffer):
    store(pod_state(Severity.WARNING, 5))

    (finding,) = build().findings

    assert finding.severity_basis == bundle.BASIS_ASSESSED


def test_the_basis_never_reaches_the_visible_facts(buffer):
    store(event(Severity.WARNING, 5))

    (finding,) = build().findings

    assert "severity_basis" not in finding.facts


# ---- ranking ----------------------------------------------------------------


def test_at_equal_level_the_assessment_leads_even_when_older(buffer):
    # The event is newer, so recency alone would put it first.
    store(pod_state(Severity.WARNING, 20), event(Severity.WARNING, 2))

    assert [f.severity_basis for f in build().findings] == [
        bundle.BASIS_ASSESSED,
        bundle.BASIS_CLUSTER,
    ]


def test_a_higher_level_still_wins_whatever_its_basis(buffer):
    store(pod_state(Severity.INFO, 2), event(Severity.WARNING, 20))

    assert [f.severity for f in build().findings] == [Severity.WARNING, Severity.INFO]


# ---- what the receipt and the reader get -----------------------------------


def test_the_basis_is_part_of_the_evidence_receipt(buffer):
    store(event(Severity.WARNING, 5))
    b = build()
    before = bundle.evidence_receipt(b)

    b.findings[0].severity_basis = bundle.BASIS_ASSESSED

    assert bundle.evidence_receipt(b) != before


def test_the_rendered_view_says_which_scale_it_is(buffer):
    store(event(Severity.WARNING, 5))

    assert "Kubernetes' own Normal/Warning label" in bundle.render(build())