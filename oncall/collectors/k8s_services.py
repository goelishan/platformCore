"""
Service endpoint state collector.

  - The mapping is pure. It takes Services and EndpointSlices that have already been
    listed and returns Signals, so every rule below is exercisable against a stub with
    no cluster. collect() is the only function here that performs I/O.
  - The first source whose subject is not something that failed. A Service with no
    endpoints is in no bad state of its own — the Service is healthy, the pods are
    healthy, and the fault is that nothing joins them. No object's status carries it,
    which is the whole reason it needs a source rather than a branch in k8s_pods.
  - Two faults, not one, and both fall out of the same object. An empty slice means
    the selector matches nothing; a populated slice with nothing ready means the pods
    are there and failing readiness. Different causes and different fixes.
  - Selectorless Services are skipped. Headless with manual endpoints, ExternalName,
    and the kubernetes Service in default all legitimately have no endpoints of their
    own, so reporting them would put permanent noise in the store.
"""

from __future__ import annotations

from typing import Any

from oncall.collectors import k8s_client as k8s
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    partition_payload,
)

# How the count was arrived at rather than what the cluster did. A Service backed by
# six slices and one backed by one are the same finding; the number matters only to a
# reader auditing this collector's arithmetic.
PROVENANCE_KEYS = frozenset({"endpoint_slices"})

# EndpointSlices name their Service with this label rather than an ownerReference,
# which is what makes the join a dictionary lookup instead of a resolver.
SERVICE_NAME_LABEL = "kubernetes.io/service-name"

NO_ENDPOINTS = "NoEndpoints"
NO_READY_ENDPOINTS = "NoReadyEndpoints"
HAS_ENDPOINTS = "HasEndpoints"


# ---- the synthetic timestamp -----------------------------------------------
# Every other source reads event_time off the thing that changed: a container's
# finishedAt, a condition's lastTransitionTime, an event's timestamp. An endpoint list
# that went empty has none of those. It is not a transition recorded anywhere; it is a
# relationship that has stopped holding, and Kubernetes stores no time for it.
#
# So event_time is the Service's creationTimestamp, which is stable and wrong: it says
# the problem began when the Service was created, when in fact the selector may have
# been broken by an edit an hour ago. The alternative is the clock, which would mint a
# new signal_id every poll for precisely the Services that are stuck — the pathology
# k8s_pods' timestamp ladder exists to prevent.
#
# What this costs, exactly. Every reason a Service passes through shares one
# event_time, so the three of them cannot be ordered against each other and recurrence
# is not observable here: a Service that broke and recovered five times is
# indistinguishable from one that broke once. What survives is current state, because
# only the current row keeps being re-collected and the writer moves collected_at
# forward each time. The newest collected_at for a subject is the state it is in now.
#
# This is the only source in the system whose event_time is derived rather than
# observed. Anything reading recurrence must exclude it.


def _slices_by_service(slices: list[Any]) -> dict[tuple[str, str], list[Any]]:
    grouped: dict[tuple[str, str], list[Any]] = {}

    for sl in slices:
        labels = sl.metadata.labels or {}
        name = labels.get(SERVICE_NAME_LABEL)
        if not name:
            # A slice with no service-name label is manually managed and belongs to
            # nothing this collector reports on.
            continue
        grouped.setdefault((sl.metadata.namespace, name), []).append(sl)

    return grouped


def _counts(slices: list[Any]) -> tuple[int, int]:
    """(total, ready) endpoints across every slice backing one Service.

    Endpoints, not addresses: one endpoint is one backing pod, and a dual-stack pod
    carries two addresses without being two of anything.

    A nil ready condition means ready, per the EndpointSlice API. Reading it as false
    would report every slice written by a controller that omits conditions as a total
    outage.
    """
    total = ready = 0

    for sl in slices:
        for ep in sl.endpoints or []:
            total += 1
            cond = getattr(ep, "conditions", None)
            if cond is None or cond.ready is None or cond.ready:
                ready += 1

    return total, ready


def _facts(total: int, ready: int) -> dict[str, Any]:
    """Which of the two faults this is, or neither.

    The split is the point of the collector. Empty means the selector matched no pods
    at all: a typo, a renamed label, a Deployment scaled to zero. Populated but none
    ready means the pods are found and every one of them is failing its readiness
    probe, which is a different incident with a different fix and is the same event
    k8s_pods reports from the pod's end.
    """
    if total == 0:
        return {"reason": NO_ENDPOINTS, "severity": Severity.ERROR}

    if ready == 0:
        return {"reason": NO_READY_ENDPOINTS, "severity": Severity.WARNING}

    return {"reason": HAS_ENDPOINTS, "severity": Severity.INFO}


# ---- signal construction ---------------------------------------------------


def signal_for_service(
    service: Any, slices: list[Any], cluster: str
) -> Signal | None:
    """None for a Service this collector does not speak for.

    Returned rather than filtered by the caller so the rule is testable without
    reaching into collect(), and so there is one place that decides what is in scope.
    """
    selector = service.spec.selector
    if not selector:
        return None

    total, ready = _counts(slices)
    facts = _facts(total, ready)

    payload: dict[str, Any] = {
        "reason": facts["reason"],
        # The thing that is wrong when the reason is NoEndpoints, and the first thing
        # a reader wants to compare against a workload's labels.
        "selector": selector,
        "service_type": service.spec.type,
        "cluster_ip": service.spec.cluster_ip,
        "ports": [
            {"name": p.name, "port": p.port, "protocol": p.protocol}
            for p in (service.spec.ports or [])
        ]
        or None,
        "endpoint_total": total,
        "endpoint_ready": ready,
        "endpoint_slices": len(slices),
    }

    return Signal(
        source=SignalSource.K8S_SERVICES,
        kind=SignalKind.SERVICE_STATE,
        cluster=cluster,
        event_time=service.metadata.creation_timestamp,
        namespace=service.metadata.namespace,
        subject=Subject(
            kind="Service", name=service.metadata.name, uid=service.metadata.uid
        ),
        # None by construction, and not an omission. A Service has no ownerReferences,
        # and resolving one from the selector is circular: the selector is the thing
        # under suspicion. The join to a workload has to happen at read time, against
        # namespace and labels, where a human or the reasoner can see it was inferred.
        owner=None,
        # Endpoints span nodes by design, so there is no single node to name. Leaving
        # it null is the honest answer rather than picking the first one.
        node=None,
        severity=facts["severity"],
        # The reason alone. The Service is already in the fingerprint through subject,
        # and the counts must stay out: a rolling update walks the ready count up and
        # down, and keying on it would mint a fresh fingerprint per replica change and
        # report one Service as a stream of unrelated problems.
        dedupe_key=facts["reason"],
        payload=partition_payload(payload, PROVENANCE_KEYS),
    )


# ---- the only I/O in this module -------------------------------------------


def collect(cluster: str) -> tuple[SourceStatus, list[Signal], str | None]:
    """Returns (status, signals, error).

    Any failure at all becomes unavailable with no signals, matching k8s_pods: a
    partial snapshot presented as a complete one is the one thing the assembler cannot
    detect. That matters more here than elsewhere, because a truncated slice list does
    not look wrong — it looks like Services that lost their endpoints.
    """
    try:
        services = k8s.list_all(k8s.core_v1().list_service_for_all_namespaces)
        slices = k8s.list_all(
            k8s.discovery_v1().list_endpoint_slice_for_all_namespaces
        )
    except Exception as exc:
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"

    grouped = _slices_by_service(slices)

    signals: list[Signal] = []
    for service in services:
        ns = service.metadata.namespace
        if not k8s.in_scope(ns):
            continue

        signal = signal_for_service(
            service, grouped.get((ns, service.metadata.name), []), cluster
        )
        if signal is not None:
            signals.append(signal)

    return (SourceStatus.OK if signals else SourceStatus.EMPTY), signals, None
