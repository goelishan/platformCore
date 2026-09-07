"""
Service endpoint state mapping tests.

The two faults this collector exists to separate look identical from outside: both
serve 503s, both show an empty endpoint list to anything that only counts ready
addresses. The distinction lives entirely in whether the slice has entries at all,
so most of this file is about that boundary.

Stubs only. The mapping never calls the API, so nothing here needs a cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oncall.collectors.k8s_services import (
    HAS_ENDPOINTS,
    NO_ENDPOINTS,
    NO_READY_ENDPOINTS,
    SERVICE_NAME_LABEL,
    _slices_by_service,
    signal_for_service,
)
from oncall.envelope import Severity, SignalKind, SignalSource, provenance_of, visible

CLUSTER = "test-cluster"
T0 = datetime(2026, 8, 10, 13, 0, 0, tzinfo=UTC)


# ---- stubs -----------------------------------------------------------------


class _Bag:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# None is a value this collector cares about, so it cannot double as "not supplied".
_UNSET = object()


def service(
    name: str = "nginx",
    *,
    namespace: str = "platformcore",
    selector: dict | None = _UNSET,
    svc_type: str = "ClusterIP",
    cluster_ip: str = "172.20.1.1",
    created: datetime = T0,
):
    return _Bag(
        metadata=_Bag(
            name=name, namespace=namespace, uid=f"uid-{name}", creation_timestamp=created
        ),
        spec=_Bag(
            selector={"app": "nginx"} if selector is _UNSET else selector,
            type=svc_type,
            cluster_ip=cluster_ip,
            ports=[_Bag(name="http", port=80, protocol="TCP")],
        ),
    )


def endpoint(ready: bool | None = True):
    return _Bag(conditions=_Bag(ready=ready), addresses=["10.0.0.1"])


def endpoint_without_conditions():
    return _Bag(conditions=None, addresses=["10.0.0.1"])


def slice_(
    *endpoints,
    service_name: str | None = "nginx",
    namespace: str = "platformcore",
    name: str = "nginx-abc12",
):
    labels = {SERVICE_NAME_LABEL: service_name} if service_name else {}
    return _Bag(
        metadata=_Bag(name=name, namespace=namespace, labels=labels),
        endpoints=list(endpoints),
    )


# ---- the two faults --------------------------------------------------------


def test_no_slices_at_all_is_no_endpoints():
    """A selector that matches nothing. The Service is healthy, the pods are healthy
    if they exist, and the fault is only in the join."""
    sig = signal_for_service(service(), [], CLUSTER)

    assert sig.payload["reason"] == NO_ENDPOINTS
    assert sig.severity == Severity.ERROR
    assert sig.payload["endpoint_total"] == 0


def test_empty_slice_is_also_no_endpoints():
    """The controller writes an empty slice rather than deleting it, so absence and
    emptiness are the same fault reached two ways. Both must key identically or one
    Service produces two fingerprints depending on cluster version."""
    absent = signal_for_service(service(), [], CLUSTER)
    empty = signal_for_service(service(), [slice_()], CLUSTER)

    assert empty.payload["reason"] == NO_ENDPOINTS
    assert absent.fingerprint == empty.fingerprint


def test_populated_but_none_ready_is_a_different_fault():
    """Pods are found and every one is failing readiness. Same 503 to a caller,
    completely different fix, and the same incident k8s_pods reports from the pod's
    end — which is only joinable if the two are not collapsed here."""
    sig = signal_for_service(
        service(), [slice_(endpoint(ready=False), endpoint(ready=False))], CLUSTER
    )

    assert sig.payload["reason"] == NO_READY_ENDPOINTS
    assert sig.severity == Severity.WARNING
    assert (sig.payload["endpoint_total"], sig.payload["endpoint_ready"]) == (2, 0)


def test_the_two_faults_do_not_share_a_fingerprint():
    none_at_all = signal_for_service(service(), [], CLUSTER)
    none_ready = signal_for_service(service(), [slice_(endpoint(ready=False))], CLUSTER)

    assert none_at_all.fingerprint != none_ready.fingerprint


def test_one_ready_endpoint_is_healthy():
    sig = signal_for_service(
        service(), [slice_(endpoint(ready=True), endpoint(ready=False))], CLUSTER
    )

    assert sig.payload["reason"] == HAS_ENDPOINTS
    assert sig.severity == Severity.INFO
    assert (sig.payload["endpoint_total"], sig.payload["endpoint_ready"]) == (2, 1)


def test_nil_ready_condition_counts_as_ready():
    """Per the EndpointSlice API a nil ready is true. Reading it as false would report
    every slice from a controller that omits conditions as a total outage."""
    for ep in (endpoint(ready=None), endpoint_without_conditions()):
        sig = signal_for_service(service(), [slice_(ep)], CLUSTER)
        assert sig.payload["reason"] == HAS_ENDPOINTS


# ---- identity --------------------------------------------------------------


def test_replica_count_does_not_change_the_fingerprint():
    """A rolling update walks the ready count up and down. If the counts reached the
    dedupe key, one Service would report as a stream of unrelated problems and the
    recurrence analysis would see them as such."""
    two = signal_for_service(service(), [slice_(endpoint(), endpoint())], CLUSTER)
    five = signal_for_service(
        service(),
        [slice_(endpoint(), endpoint(), endpoint(), endpoint(), endpoint())],
        CLUSTER,
    )

    assert two.fingerprint == five.fingerprint


def test_event_time_is_the_service_creation_timestamp():
    """Synthetic and stable. Stable is what matters: the clock would mint a fresh
    signal_id every poll for precisely the Services that are stuck."""
    sig = signal_for_service(service(created=T0), [], CLUSTER)

    assert sig.event_time == T0
    assert sig.signal_id == signal_for_service(service(created=T0), [], CLUSTER).signal_id


def test_two_services_in_one_namespace_are_two_fingerprints():
    a = signal_for_service(service(name="nginx"), [], CLUSTER)
    b = signal_for_service(service(name="fastapi"), [], CLUSTER)

    assert a.fingerprint != b.fingerprint


def test_owner_and_node_are_null_by_construction():
    """Not omissions. A Service has no ownerReferences and resolving one from the
    selector is circular; endpoints span nodes so there is no single node to name."""
    sig = signal_for_service(service(), [slice_(endpoint())], CLUSTER)

    assert sig.owner is None
    assert sig.node is None


def test_envelope_fields():
    sig = signal_for_service(service(), [], CLUSTER)

    assert sig.source == SignalSource.K8S_SERVICES
    assert sig.kind == SignalKind.SERVICE_STATE
    assert sig.subject.kind == "Service"
    assert sig.subject.name == "nginx"
    assert sig.namespace == "platformcore"


# ---- scope -----------------------------------------------------------------


def test_selectorless_service_produces_nothing():
    """ExternalName, manually managed endpoints, and the kubernetes Service in default
    all legitimately have no endpoints of their own. Reporting them would be permanent
    noise in the store."""
    assert signal_for_service(service(selector=None), [], CLUSTER) is None
    assert signal_for_service(service(selector={}), [], CLUSTER) is None


# ---- payload ---------------------------------------------------------------


def test_slice_count_is_provenance_not_evidence():
    """One slice or six is the same finding. The number is only of interest to a
    reader auditing the arithmetic, so it must not reach the reasoner."""
    sig = signal_for_service(
        service(),
        [slice_(endpoint()), slice_(endpoint(), name="nginx-def34")],
        CLUSTER,
    )

    assert provenance_of(sig.payload)["endpoint_slices"] == 2
    assert "endpoint_slices" not in visible(sig.payload)


def test_selector_travels_in_the_payload():
    """The thing that is wrong when the reason is NoEndpoints, and the first value a
    reader compares against a workload's labels."""
    sig = signal_for_service(service(selector={"app": "typo"}), [], CLUSTER)

    assert visible(sig.payload)["selector"] == {"app": "typo"}


# ---- grouping --------------------------------------------------------------


def test_slices_group_by_namespace_and_service_name():
    """Two Services of the same name in different namespaces must not pool their
    endpoints, which would report a broken one as healthy."""
    grouped = _slices_by_service(
        [
            slice_(endpoint(), namespace="a", name="nginx-1"),
            slice_(endpoint(), namespace="b", name="nginx-2"),
        ]
    )

    assert set(grouped) == {("a", "nginx"), ("b", "nginx")}
    assert len(grouped[("a", "nginx")]) == 1


def test_unlabelled_slice_belongs_to_nothing():
    """A slice with no service-name label is manually managed. Attributing it to a
    Service by guessing would report an outage as healthy."""
    assert _slices_by_service([slice_(endpoint(), service_name=None)]) == {}
