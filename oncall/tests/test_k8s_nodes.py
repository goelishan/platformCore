"""
Node state mapping tests.

The rule this file mostly guards is that healthy is not a single value. Ready is good
when True and every pressure condition is good when False, so a collector that assumes
one polarity reports either a dying node as fine or a healthy one as failing, and both
readings are confident.

Stubs only. The mapping never calls the API, so nothing here needs a cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncall.collectors.k8s_nodes import signals_for_node
from oncall.envelope import Severity, SignalKind, SignalSource, visible

CLUSTER = "test-cluster"
T0 = datetime(2026, 8, 10, 13, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=3)


# ---- stubs -----------------------------------------------------------------


class _Bag:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def condition(type_: str, status: str, *, reason="KubeletReady", message="ok", at=T0):
    return _Bag(
        type=type_, status=status, reason=reason, message=message, last_transition_time=at
    )


def taint(key: str, effect: str, value: str | None = None):
    return _Bag(key=key, value=value, effect=effect)


def node(
    name: str = "ip-10-0-10-194.ec2.internal",
    *,
    conditions=None,
    unschedulable=None,
    taints=None,
    labels=None,
):
    return _Bag(
        metadata=_Bag(
            name=name,
            uid=f"uid-{name}",
            labels={
                "node.kubernetes.io/instance-type": "t3.small",
                "topology.kubernetes.io/zone": "us-east-1a",
            }
            if labels is None
            else labels,
        ),
        spec=_Bag(unschedulable=unschedulable, taints=taints),
        status=_Bag(
            conditions=[condition("Ready", "True")] if conditions is None else conditions,
            allocatable={"cpu": "1930m", "memory": "1466544Ki", "pods": "110"},
            capacity={"cpu": "2", "memory": "2004848Ki", "pods": "110"},
            node_info=_Bag(kubelet_version="v1.33.0-eks-1234567"),
        ),
    )


def only(signals, cond_type):
    return next(s for s in signals if s.payload["condition"] == cond_type)


# ---- polarity --------------------------------------------------------------


def test_ready_true_is_healthy_and_pressure_false_is_healthy():
    """The whole point of the polarity map. A collector that read True as good would
    report a node with no memory pressure as a warning on every poll."""
    sigs = signals_for_node(
        node(
            conditions=[
                condition("Ready", "True"),
                condition("MemoryPressure", "False"),
                condition("DiskPressure", "False"),
                condition("PIDPressure", "False"),
            ]
        ),
        CLUSTER,
    )

    assert {s.severity for s in sigs} == {Severity.INFO}


def test_ready_false_is_critical():
    sigs = signals_for_node(node(conditions=[condition("Ready", "False")]), CLUSTER)

    assert only(sigs, "Ready").severity == Severity.CRITICAL


def test_ready_unknown_is_critical_not_milder():
    """A node death does not report itself as broken, it stops reporting. Treating
    Unknown as a lesser state would rank the real failure mode below the rare one."""
    sigs = signals_for_node(node(conditions=[condition("Ready", "Unknown")]), CLUSTER)

    assert only(sigs, "Ready").severity == Severity.CRITICAL


def test_pressure_true_is_a_warning_not_critical():
    """The node is struggling, not gone. Pods on it are still running."""
    sigs = signals_for_node(
        node(conditions=[condition("DiskPressure", "True")]), CLUSTER
    )

    assert only(sigs, "DiskPressure").severity == Severity.WARNING


def test_unrecognised_condition_gets_no_opinion():
    """Cloud providers and add-ons attach their own types. Guessing a polarity for one
    this collector has never seen produces confident nonsense in whichever direction
    the guess fell, so it reports the status and stays quiet."""
    sigs = signals_for_node(
        node(
            conditions=[
                condition("VendorThing", "True"),
                condition("VendorThing2", "False"),
            ]
        ),
        CLUSTER,
    )

    assert {s.severity for s in sigs} == {Severity.INFO}
    assert only(sigs, "VendorThing").payload["status"] == "True"


# ---- one signal per condition ----------------------------------------------


def test_a_node_produces_one_signal_per_condition():
    """Ready while under DiskPressure is two facts. Collapsing them to one node-level
    signal loses the one that matters and forces a choice between two real
    lastTransitionTime values."""
    sigs = signals_for_node(
        node(
            conditions=[
                condition("Ready", "True", at=T0),
                condition("DiskPressure", "True", at=T1),
            ]
        ),
        CLUSTER,
    )

    assert len(sigs) == 2
    assert only(sigs, "Ready").event_time == T0
    assert only(sigs, "DiskPressure").event_time == T1


def test_conditions_of_one_node_do_not_share_a_fingerprint():
    sigs = signals_for_node(
        node(
            conditions=[condition("Ready", "True"), condition("MemoryPressure", "False")]
        ),
        CLUSTER,
    )

    assert len({s.fingerprint for s in sigs}) == 2


def test_a_transition_mints_a_new_fingerprint():
    """Unlike the Service source, this one has a real transition timestamp, so the two
    rows order against each other and a flapping node reads as a flapping node."""
    up = only(
        signals_for_node(node(conditions=[condition("Ready", "True", at=T0)]), CLUSTER),
        "Ready",
    )
    down = only(
        signals_for_node(node(conditions=[condition("Ready", "False", at=T1)]), CLUSTER),
        "Ready",
    )

    assert up.fingerprint != down.fingerprint
    assert up.event_time != down.event_time


def test_condition_reason_stays_out_of_the_key():
    """reason and status move together, so it adds nothing, and a kubelet release that
    reworded it would split one healthy node into two problems."""
    a = only(
        signals_for_node(
            node(conditions=[condition("Ready", "True", reason="KubeletReady")]), CLUSTER
        ),
        "Ready",
    )
    b = only(
        signals_for_node(
            node(conditions=[condition("Ready", "True", reason="KubeletIsReady")]),
            CLUSTER,
        ),
        "Ready",
    )

    assert a.fingerprint == b.fingerprint
    assert b.payload["reason"] == "KubeletIsReady"


# ---- envelope --------------------------------------------------------------


def test_the_node_names_itself():
    """What lets a node's own health row join to the pod rows carrying its name, which
    is the reason this collector exists."""
    sigs = signals_for_node(node(name="ip-10-0-11-113.ec2.internal"), CLUSTER)

    assert sigs[0].node == "ip-10-0-11-113.ec2.internal"
    assert sigs[0].subject.name == "ip-10-0-11-113.ec2.internal"


def test_cluster_scoped_so_no_namespace_and_no_owner():
    sigs = signals_for_node(node(), CLUSTER)

    assert sigs[0].namespace is None
    assert sigs[0].owner is None


def test_envelope_fields():
    sigs = signals_for_node(node(), CLUSTER)

    assert sigs[0].source == SignalSource.K8S_NODES
    assert sigs[0].kind == SignalKind.NODE_STATE
    assert sigs[0].subject.kind == "Node"


# ---- payload ---------------------------------------------------------------


def test_every_row_carries_capacity_so_it_reads_alone():
    """The assembler hands single rows to the reasoner. A row that needs a join to be
    legible is a row that will be read without one."""
    sigs = signals_for_node(
        node(conditions=[condition("Ready", "True"), condition("DiskPressure", "True")]),
        CLUSTER,
    )

    for sig in sigs:
        assert visible(sig.payload)["allocatable_cpu"] == "1930m"
        assert visible(sig.payload)["allocatable_pods"] == "110"


def test_quantities_are_not_parsed():
    """Kept as Kubernetes wrote them. A units parser for the quantity grammar is a bug
    farm, and the consumer that has to compare them reads 1930m correctly."""
    sig = signals_for_node(node(), CLUSTER)[0]

    assert visible(sig.payload)["allocatable_memory"] == "1466544Ki"
    assert visible(sig.payload)["capacity_cpu"] == "2"


def test_cordon_and_taints_travel_as_context():
    """Frequently the whole explanation for a Pending pod elsewhere in the store, and a
    spec field with no transition time of its own, so it rides on the condition row
    rather than inventing a signal with a made-up timestamp."""
    sig = signals_for_node(
        node(
            unschedulable=True,
            taints=[taint("node.kubernetes.io/unschedulable", "NoSchedule")],
        ),
        CLUSTER,
    )[0]

    assert visible(sig.payload)["unschedulable"] is True
    assert visible(sig.payload)["taints"] == [
        "node.kubernetes.io/unschedulable:NoSchedule"
    ]


def test_a_taint_with_a_value_renders_with_it():
    sig = signals_for_node(
        node(taints=[taint("dedicated", "NoSchedule", value="gpu")]), CLUSTER
    )[0]

    assert visible(sig.payload)["taints"] == ["dedicated=gpu:NoSchedule"]


def test_placement_travels_for_the_availability_zone_question():
    """Every failure on one node and every failure in one zone are different
    diagnoses, and the second is invisible without this."""
    sig = signals_for_node(node(), CLUSTER)[0]

    assert visible(sig.payload)["zone"] == "us-east-1a"
    assert visible(sig.payload)["instance_type"] == "t3.small"
    assert visible(sig.payload)["kubelet_version"] == "v1.33.0-eks-1234567"


def test_a_node_with_no_conditions_produces_nothing():
    """Not a crash. A node object that has not yet reported conditions is a real
    transient state during bootstrap."""
    assert signals_for_node(node(conditions=[]), CLUSTER) == []
