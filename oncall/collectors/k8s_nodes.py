"""
Node state collector.

  - The mapping is pure. It takes nodes that have already been listed and returns
    Signals, so every rule below is exercisable against a stub with no cluster.
    collect() is the only function here that performs I/O.
  - One signal per condition, not per node. A node that is Ready while under
    DiskPressure is two facts, and blurring them loses the one that matters — the same
    argument that makes k8s_pods emit per container. It also keeps event_time honest:
    each condition carries its own lastTransitionTime, and a single node-level signal
    would have to pick one of them.
  - Which way is healthy depends on the condition. Ready is good when True; every
    pressure condition is good when False. A type this collector does not recognise
    gets no opinion at all rather than a guess.
  - Rows are self-contained. Capacity and allocatable repeat on every condition row of
    a node rather than being carried once, because the assembler hands single rows to
    the reasoner and a row that needs a join to be legible is a row that will be read
    without one.

This is the source that makes Signal.node mean something. Three collectors already
stamp a node name onto every signal, deliberately, so that five failures on one node
can be told from five failures across five. Until now nothing said whether that node
was healthy, so the GROUP BY could point at a node and never explain it.
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
    redact,
)

# The status each condition holds when nothing is wrong. Ready is the odd one out, and
# hardcoding "True is good" would report a node with no memory pressure as a fault.
#
# A condition absent from this map is reported with severity INFO and its status in the
# payload. Cloud providers and add-ons attach their own condition types, and inventing
# a polarity for one this collector has never seen would produce confident nonsense in
# whichever direction the guess fell.
HEALTHY_STATUS = {
    "Ready": "True",
    "MemoryPressure": "False",
    "DiskPressure": "False",
    "PIDPressure": "False",
    "NetworkUnavailable": "False",
}

READY = "Ready"


# ---- severity --------------------------------------------------------------


def _severity(cond_type: str, status: str) -> Severity:
    """Ready failing is the node gone; a pressure condition is the node struggling.

    Ready=Unknown is not milder than Ready=False. It means the kubelet stopped posting
    status at all, which is how a node death actually presents: the node does not
    report that it is broken, it stops reporting.
    """
    healthy = HEALTHY_STATUS.get(cond_type)

    if healthy is None:
        return Severity.INFO

    if status == healthy:
        return Severity.INFO

    return Severity.CRITICAL if cond_type == READY else Severity.WARNING


# ---- what travels with every condition -------------------------------------


def _taints(node: Any) -> list[str] | None:
    """Rendered as strings rather than objects, because their only use here is being
    read. A cordoned node carries node.kubernetes.io/unschedulable:NoSchedule, which is
    frequently the whole explanation for a Pending pod elsewhere in the store."""
    taints = getattr(node.spec, "taints", None) or []
    rendered = [
        f"{t.key}={t.value}:{t.effect}" if t.value else f"{t.key}:{t.effect}"
        for t in taints
    ]
    return rendered or None


def _resources(node: Any) -> dict[str, Any]:
    """Quantities are kept as Kubernetes wrote them: 1930m, 1466544Ki.

    Not parsed into numbers. A units parser for the quantity grammar is a bug farm, the
    strings are what kubectl shows and what a reader will compare against, and the one
    consumer that has to do arithmetic on them is a language model that reads 1930m
    correctly. Converting would trade a real risk of being wrong for no gain.
    """
    allocatable = node.status.allocatable or {}
    capacity = node.status.capacity or {}

    return {
        "allocatable_cpu": allocatable.get("cpu"),
        "allocatable_memory": allocatable.get("memory"),
        "allocatable_pods": allocatable.get("pods"),
        "capacity_cpu": capacity.get("cpu"),
        "capacity_memory": capacity.get("memory"),
        "capacity_pods": capacity.get("pods"),
    }


def _placement(node: Any) -> dict[str, Any]:
    """Instance type and zone, because "every failure is on one node" and "every
    failure is in one availability zone" are different diagnoses and the second is
    invisible without this."""
    labels = node.metadata.labels or {}
    info = node.status.node_info

    return {
        "instance_type": labels.get("node.kubernetes.io/instance-type"),
        "zone": labels.get("topology.kubernetes.io/zone"),
        "kubelet_version": getattr(info, "kubelet_version", None) if info else None,
        # A spec field, not a condition, so it has no transition time of its own and
        # cannot honestly be a signal. It rides here instead: the current value is
        # always right because the writer refreshes the payload on re-collection, and
        # only the moment of cordoning goes unrecorded.
        "unschedulable": node.spec.unschedulable or None,
        "taints": _taints(node),
    }


# ---- signal construction ---------------------------------------------------


def signals_for_node(node: Any, cluster: str) -> list[Signal]:
    resources = _resources(node)
    placement = _placement(node)
    signals: list[Signal] = []

    for cond in node.status.conditions or []:
        message, message_redacted = redact(cond.message)

        payload: dict[str, Any] = {
            "condition": cond.type,
            "status": cond.status,
            # Stable per (type, status) and deliberately out of the key. A kubelet
            # release that reworded KubeletHasSufficientMemory would otherwise split
            # one healthy node into two problems.
            "reason": cond.reason,
            "message": message or None,
            **resources,
            **placement,
        }

        signals.append(
            Signal(
                source=SignalSource.K8S_NODES,
                kind=SignalKind.NODE_STATE,
                cluster=cluster,
                # Real, unlike the Service source: the kubelet records when each
                # condition last flipped, so a node that flaps produces genuinely
                # distinct occurrences and recurrence analysis works here.
                event_time=cond.last_transition_time,
                # Nodes are cluster-scoped. Leaving this null rather than inventing a
                # namespace keeps the column meaning one thing.
                namespace=None,
                subject=Subject(
                    kind="Node", name=node.metadata.name, uid=node.metadata.uid
                ),
                # Nodes have no ownerReferences. A managed node group owns them in AWS,
                # which Kubernetes does not model and this collector will not infer.
                owner=None,
                # The node names itself. That is what lets a node's own health row join
                # to the pod rows that carry its name, which is the entire reason this
                # collector exists.
                node=node.metadata.name,
                severity=_severity(cond.type, cond.status),
                # Type and status. A transition mints a new fingerprint and carries its
                # own lastTransitionTime, so the two rows order correctly against each
                # other and a flapping node is visible as a flapping node.
                dedupe_key=f"{cond.type}|{cond.status}",
                redacted=message_redacted,
                payload=partition_payload(payload, frozenset()),
            )
        )

    return signals


# ---- the only I/O in this module -------------------------------------------


def collect(cluster: str) -> tuple[SourceStatus, list[Signal], str | None]:
    """Returns (status, signals, error).

    Any failure at all becomes unavailable with no signals, matching the other
    collectors. A truncated node list is the worst possible partial result here: the
    nodes it omits are indistinguishable from nodes that have left the cluster.
    """
    try:
        nodes = k8s.list_all(k8s.core_v1().list_node)
    except Exception as exc:
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"

    signals: list[Signal] = []
    for node in nodes:
        signals.extend(signals_for_node(node, cluster))

    return (SourceStatus.OK if signals else SourceStatus.EMPTY), signals, None
