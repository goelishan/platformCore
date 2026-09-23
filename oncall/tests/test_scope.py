"""
Scope tests: which rows one subject's bundle admits, and under which rule.

  - The shapes are the 2026-09-07 corpus: a pod Pending because both workers carried
    the not-ready taint, its ReplicaSet and Deployment events, and three nodes. Before
    scoping, that pod's bundle held four findings and none of them was the cause.
  - Every case goes through the real writer and build(), because membership is decided
    over buffer rows and a fake row proves nothing about the columns it reads.
  - Membership is asserted in both directions. A rule that admits too much is as
    wrong as one that admits too little, and only the second is visible by eye.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncall import landing_zone as lz
from oncall.envelope import (
    Owner,
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
)
from oncall.evidence import bundle
from oncall.evidence.scope import (
    JOIN_CLUSTER,
    JOIN_NAMESPACE,
    JOIN_NODE,
    JOIN_OWNER,
    JOIN_SUBJECT,
    JOIN_UNSCHEDULED,
    Scope,
)

CLUSTER = "test-cluster"
NS = "lab"
POD = "crashloop-77d8-j886l"
RS = "crashloop-77d8"
DEPLOY = "crashloop"

NOW = datetime.now(UTC).replace(microsecond=0)
START = NOW - timedelta(minutes=30)
END = NOW + timedelta(minutes=1)


# ---- helpers ---------------------------------------------------------------


def sig(
    source: SignalSource,
    kind: SignalKind,
    subject_kind: str,
    name: str,
    *,
    namespace: str | None = NS,
    owner: str | None = DEPLOY,
    node: str | None = None,
    severity: Severity = Severity.INFO,
    key: str = "k",
) -> Signal:
    return Signal(
        source=source,
        kind=kind,
        cluster=CLUSTER,
        event_time=NOW - timedelta(minutes=5),
        namespace=namespace,
        subject=Subject(kind=subject_kind, name=name, uid=f"uid-{name}"),
        owner=Owner(kind="Deployment", name=owner) if owner else None,
        node=node,
        severity=severity,
        dedupe_key=key,
    )


def pod(name: str = POD, node: str | None = None, namespace: str = NS, owner: str = DEPLOY):
    return sig(
        SignalSource.K8S_PODS, SignalKind.POD_STATE, "Pod", name,
        namespace=namespace, owner=owner, node=node, severity=Severity.ERROR,
    )


def event(subject_kind: str, name: str, owner: str | None = DEPLOY):
    return sig(SignalSource.K8S_EVENTS, SignalKind.EVENT, subject_kind, name, owner=owner)


def node(name: str, severity: Severity = Severity.INFO):
    return sig(
        SignalSource.K8S_NODES, SignalKind.NODE_STATE, "Node", name,
        namespace=None, owner=None, node=name, severity=severity,
    )


def service(name: str, namespace: str = NS):
    return sig(
        SignalSource.K8S_SERVICES, SignalKind.SERVICE_STATE, "Service", name,
        namespace=namespace, owner=None, severity=Severity.WARNING,
    )


def store(*signals: Signal) -> None:
    by_source: dict[SignalSource, list[Signal]] = {}
    for s in signals:
        by_source.setdefault(s.source, []).append(s)
    with lz.connect() as conn:
        for source, batch in by_source.items():
            run = lz.start_run(conn, source, CLUSTER)
            lz.write_signals(conn, run, batch)
            lz.finish_run(conn, run, SourceStatus.OK, len(batch))


def build(subject: str | None) -> bundle.Bundle:
    with lz.connect() as conn:
        return bundle.build(conn, CLUSTER, START, END, subject_name=subject, history=False)


def joins(b: bundle.Bundle) -> dict[str | None, str | None]:
    return {f.subject_name: f.joined_by for f in b.findings}


def scope_of(b: bundle.Bundle) -> Scope:
    """The bundle's scope, failing loudly if a subject-scoped build produced none."""
    assert b.scope is not None
    return b.scope


# ---- the case that motivated it ---------------------------------------------


def test_an_unscheduled_pod_admits_every_node_that_could_explain_it(buffer):
    store(
        pod(),
        node("worker", Severity.CRITICAL),
        node("worker2", Severity.CRITICAL),
        node("control-plane"),
    )

    b = build(POD)

    assert joins(b) == {
        POD: JOIN_SUBJECT,
        "worker": JOIN_UNSCHEDULED,
        "worker2": JOIN_UNSCHEDULED,
        "control-plane": JOIN_UNSCHEDULED,
    }
    assert b.findings[0].severity == Severity.CRITICAL
    assert scope_of(b).unscheduled is True


def test_a_placed_pod_admits_its_own_node_and_no_other(buffer):
    store(pod(node="worker"), node("worker"), node("worker2", Severity.CRITICAL))

    b = build(POD)

    assert joins(b) == {POD: JOIN_SUBJECT, "worker": JOIN_NODE}
    assert scope_of(b).unscheduled is False


def test_events_without_a_node_do_not_make_a_placed_workload_unscheduled(buffer):
    # Events leave node_name empty by construction. Reading that as "on no node"
    # would pull the whole fleet into every noisy workload's bundle.
    store(pod(node="worker"), event("Pod", POD), node("worker2", Severity.CRITICAL))

    b = build(POD)

    assert "worker2" not in joins(b)
    assert scope_of(b).unscheduled is False


# ---- owner -----------------------------------------------------------------


def test_the_owner_brings_in_controller_events_and_sibling_pods(buffer):
    store(
        pod(node="worker"),
        pod("crashloop-77d8-other", node="worker"),
        event("ReplicaSet", RS),
        event("Deployment", DEPLOY),
    )

    assert joins(build(POD)) == {
        POD: JOIN_SUBJECT,
        "crashloop-77d8-other": JOIN_OWNER,
        RS: JOIN_OWNER,
        DEPLOY: JOIN_OWNER,
    }


def test_naming_the_workload_resolves_through_its_owner(buffer):
    store(pod(node="worker"), event("ReplicaSet", RS))

    b = build(DEPLOY)

    assert scope_of(b).found is True
    assert set(joins(b)) == {POD, RS}


def test_an_owner_of_the_same_name_in_another_namespace_stays_out(buffer):
    store(pod(node="worker"), pod("crashloop-x", node="worker", namespace="other"))

    assert set(joins(build(POD))) == {POD}


# ---- namespace and absence --------------------------------------------------


def test_a_service_is_admitted_by_namespace_and_labelled_as_such(buffer):
    store(pod(node="worker"), service("api"), service("elsewhere", namespace="other"))

    assert joins(build(POD)) == {POD: JOIN_SUBJECT, "api": JOIN_NAMESPACE}


def test_an_unrelated_workload_stays_out(buffer):
    store(pod(node="worker"), pod("oomkill-1", node="worker2", owner="oomkill"))

    assert set(joins(build(POD))) == {POD}


def test_a_subject_with_no_rows_says_so_instead_of_reading_as_healthy(buffer):
    store(pod(node="worker"))

    b = build("nosuchpod")

    assert b.findings == []
    assert scope_of(b).found is False
    assert any("nosuchpod" in note for note in b.degraded)


def test_no_subject_is_the_whole_cluster(buffer):
    store(pod(node="worker"), node("worker2"))

    assert set(joins(build(None)).values()) == {JOIN_CLUSTER}


# ---- what the caller and the receipt get -----------------------------------


def test_candidates_are_every_admitted_signal_not_only_the_kept_findings(buffer):
    store(pod(), node("worker"), node("worker2"), pod("unrelated", owner="other"))

    with lz.connect() as conn:
        b = bundle.build(
            conn, CLUSTER, START, END, subject_name=POD, history=False, max_findings=1
        )

    assert len(b.findings) == 1
    assert len(b.candidate_signal_ids) == 3


def test_the_scope_rule_is_part_of_the_evidence_receipt(buffer):
    store(pod(), node("worker"))
    before = bundle.evidence_receipt(build(POD))

    b = build(POD)
    b.findings[-1].joined_by = JOIN_NAMESPACE

    assert bundle.evidence_receipt(b) != before