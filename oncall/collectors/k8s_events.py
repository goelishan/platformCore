"""
Kubernetes event collector.

  - Events are a stream, not state. The API server garbage-collects them at
    --event-ttl (one hour by default), so a missed poll loses them permanently.
    Pod state can always be re-read; this cannot. Everything below follows from
    that: the write path stays maximally faithful, and every judgement that can
    be deferred to read time is deferred.
  - The object arrives already collapsed. count / firstTimestamp / lastTimestamp
    is a write-time aggregation that destroyed the individual occurrence times,
    and no amount of care here recovers them. Keying event_time to the last
    observed occurrence recovers spacing at poll resolution and stops the loss
    there; count travels in the payload so the assembler can see how much of the
    window went unsampled.
  - The mapping is pure. collect() is the only function that performs I/O, so
    every rule here is exercisable against a stub with no cluster.
  - Severity mirrors the event's own type rather than a curated escalation list.
    Reason strings are not a stable API — operators mint their own — and a
    hardcoded list would read a novel failure as less serious than a known one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from oncall.collectors import k8s_client as k8s
from oncall.collectors.owners import INTERMEDIATE_KINDS, OwnerResolver
from oncall.envelope import (
    Owner,
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    iso,
    partition_payload,
    redact,
    template_of,
)

# event_uid identifies the object doing the counting, and component identifies the
# thing that reported it. Both are provenance in the strict sense: they describe the
# observation rather than the cluster, and component is already excluded from
# dedupe_key on the same grounds.
#
# event_uid is hidden rather than dropped because the assembler cannot do counter
# arithmetic without it. Aggregation is client-side, so a kubelet restart abandons
# one counter and starts another from one; diffing across that boundary invents a
# drop that never happened. Grouping by uid first is what makes the delta real.
PROVENANCE_KEYS = frozenset({"event_uid", "component"})


WARNING_TYPE = "Warning"

# Normal events worth keeping: something a controller or an operator did. The rest
# of the Normal stream is routine container lifecycle — Pulling, Pulled, Created,
# Started — which the kubelet emits on every restart and which pod state already
# reports more precisely. Warnings are kept unconditionally, with no allowlist: an
# unfamiliar Warning is the most interesting row in the table, while an unfamiliar
# Normal is almost certainly noise.
KEEP_NORMAL_REASONS = {
    "Scheduled",
    "Killing",
    "Preempted",
    "Completed",
    "ScalingReplicaSet",
    "SuccessfulCreate",
    "SuccessfulDelete",
    "SuccessfulRescale",
    "NodeReady",
    "NodeNotReady",
    "NodeHasSufficientMemory",
    "NodeHasDiskPressure",
}

# Kinds that need a lookup before the owner is known. A Pod hops through its
# ReplicaSet, a ReplicaSet through its Deployment. Anything else the event names —
# Deployment, StatefulSet, DaemonSet, Node, PVC, Ingress — is already the root and
# is its own owner, which keeps the owner join uniform across sources.
LOOKUP_KINDS = {"Pod"} | INTERMEDIATE_KINDS

# Messages are free text with no server-side bound. Truncation happens here rather
# than in the reasoner because an oversized row costs storage on every poll, while
# the diagnostic content of an event message is front-loaded.
MAX_MESSAGE_CHARS = 1024


# ---- timestamp ladder ------------------------------------------------------
# Two generations of the events API write into the same storage, so one list call
# returns a mixture. Components using events.k8s.io/v1 populate eventTime and a
# series, and leave the deprecated first/lastTimestamp pair null; older components
# do the reverse. No single field is reliably present, so the ladder is the normal
# path here rather than the exceptional one.
#
# series.lastObservedTime sits at the top because for an aggregating event it is
# the truest statement of when the problem last occurred. creationTimestamp is the
# floor: every object has one, so the ladder never falls through to the clock.
#
# Reading the clock would set event_time equal to collected_at, collapsing the two
# timestamps the envelope deliberately keeps apart, and would mint a fresh
# signal_id every poll — one new row per cycle for an event that has not changed.


def _event_time(ev: Any) -> datetime:
    series = getattr(ev, "series", None)
    if series is not None and series.last_observed_time:
        return series.last_observed_time

    return (
        ev.last_timestamp
        or ev.event_time
        or ev.first_timestamp
        or ev.metadata.creation_timestamp
    )


def _occurrence_count(ev: Any) -> int | None:
    """The API server's own aggregate. Recorded, never trusted as a timeline: it
    says how many times the problem happened, not when. A jump of five between two
    polls is how the assembler learns that a window went unsampled."""
    series = getattr(ev, "series", None)
    if series is not None and series.count:
        return series.count
    return ev.count


# ---- filtering -------------------------------------------------------------


def _should_keep(ev: Any) -> bool:
    if ev.type == WARNING_TYPE:
        return True
    return ev.reason in KEEP_NORMAL_REASONS


def _namespace_of(ev: Any) -> str | None:
    """The subject's namespace, never the event object's own.

    Events about cluster-scoped objects carry an empty involvedObject.namespace but
    are themselves stored in 'default'. Filtering on the event's namespace would
    therefore drop every Node event the moment an allowlist is configured without
    'default' — and node events are what explain a cluster-wide incident. Storing
    it would be worse: it asserts a Node lives in a namespace, which is false and
    corrupts every namespace-filtered query.
    """
    return ev.involved_object.namespace or None


# ---- subject context -------------------------------------------------------
# involvedObject is a reference, not an object, so there is no ownerReferences
# chain to walk. The owner has to be recovered by looking the subject up among the
# objects listed alongside the events.
#
# Owner is not cosmetic: idx_sig_owner and signals_in_window(owner_name=...) are
# how a pod crash correlates with the rollout that caused it. A signal with no
# owner cannot participate in that join at all.


def _resolve_subject(
    ev: Any, objects_by_uid: dict[str, Any], resolver: OwnerResolver
) -> tuple[Owner | None, str | None, str | None]:
    """Returns (owner, node, resolution_marker).

    The marker is what keeps three different absences from reading alike:

      None             owner is present, or the subject is genuinely unowned
      'partial'        the intermediate object was garbage collected mid-rollout
      'subject_gone'   the subject itself is deleted; the event outlived it
      'no_subject_uid' the event names no subject uid, so no lookup is possible

    'subject_gone' is the highest-value case in this source, not an error: the most
    useful event in an incident is often the one describing a pod that no longer
    exists. Parsing the owner out of the pod name would resolve it, and is refused
    for the same reason owners.py keys on uid — names get reused, and inheriting a
    deleted workload's history is the one way first_seen_ever() can lie.
    """
    ref = ev.involved_object
    kind = ref.kind

    if kind not in LOOKUP_KINDS:
        node = ref.name if kind == "Node" else None
        return Owner(kind=kind, name=ref.name), node, None

    if not ref.uid:
        return None, None, "no_subject_uid"

    obj = objects_by_uid.get(ref.uid)
    if obj is None:
        return None, None, "subject_gone"

    owner, resolved = resolver.resolve(obj)

    # Only a Pod carries a node assignment. Recording it lets an event join to the
    # node-scoped index, which is what separates "this workload is broken" from
    # "everything on this node is broken".
    node = obj.spec.node_name if kind == "Pod" else None

    return owner, node, None if resolved else "partial"


# ---- signal construction ---------------------------------------------------


def _container_from_field_path(field_path: str | None) -> str:
    """'spec.containers{app}' -> 'app'.

    Empty for anything not scoped to a container — a pod-level event, or any event
    about a Node or a PVC.
    """
    if not field_path or "{" not in field_path:
        return ""
    return field_path.split("{", 1)[1].rstrip("}")


def _dedupe_key(container: str, reason: str | None, message_key: str) -> str:
    """container|reason|message_template.

    The container half mirrors the pod collector's one-signal-per-container rule:
    two containers in one pod failing their readiness probes are two problems that
    happen to share a reason.

    The reporting component is deliberately absent. It answers who told us, which is
    provenance rather than identity — the same argument that keeps node out of the
    fingerprint. Including it would split one problem in two the moment a different
    component reported the same condition.

    The message is present only as a template. Raw, it carries volatile tokens —
    back-off intervals, addresses, node names, counts — so every occurrence would be
    unique, every recurrence group would have size one, and read-time collapse would
    find nothing to collapse. Masked, what is left is the part that repeats, which is
    what separates a liveness probe timing out from the same container failing to
    resolve a hostname: two conditions that share the reason 'Unhealthy' and nothing
    else.
    """
    return f"{container}|{reason or ''}|{message_key}"


def _component_of(ev: Any) -> str | None:
    if ev.source is not None and ev.source.component:
        return ev.source.component
    return getattr(ev, "reporting_component", None) or None


def signal_for_event(
    ev: Any,
    objects_by_uid: dict[str, Any],
    resolver: OwnerResolver,
    cluster: str,
) -> Signal:
    """One event is one fact, so one Signal comes back — unlike a pod, which fans
    out to one Signal per container."""
    owner, node, marker = _resolve_subject(ev, objects_by_uid, resolver)
    container = _container_from_field_path(ev.involved_object.field_path)

    # Redacted before truncation, and templated before both. Truncating first could cut
    # a credential in half and store the surviving fragment; templating the full text
    # keeps the key stable no matter where the cut lands, so the storage bound never
    # becomes an identity decision.
    message, message_redacted = redact(ev.message or None)
    template = template_of(message)

    payload: dict[str, Any] = {
        "reason": ev.reason,
        "message": message[:MAX_MESSAGE_CHARS] if message else None,
        "message_template": template.text or None,
        "message_truncated": bool(message and len(message) > MAX_MESSAGE_CHARS) or None,
        "type": ev.type,
        "container": container or None,
        # The aggregate, the window it accumulated over, and — critically — which
        # object did the counting. Aggregation is client-side and cached in the
        # reporting component's memory, so a kubelet restart abandons the old object
        # and starts a second one counting the same condition from one. Both stay
        # live, and both map here to the same fingerprint.
        #
        # Without the object identity a fingerprint's counts arrive from an unknown
        # number of independent counters and no arithmetic over them means anything.
        # With it, the reader groups by event_uid, diffs inside a group, and treats a
        # new uid as a series starting at zero — the same reset handling a counter
        # metric needs when the process exporting it restarts.
        #
        # It stays out of the fingerprint deliberately: keying on it would split one
        # ongoing problem in two the moment a node rebooted, which is precisely the
        # fragmentation the content-derived fingerprint exists to prevent.
        "count": _occurrence_count(ev),
        "event_uid": ev.metadata.uid,
        "api_first_timestamp": iso(ev.first_timestamp) if ev.first_timestamp else None,
        "component": _component_of(ev),
        "owner_resolution": marker,
    }

    return Signal(
        source=SignalSource.K8S_EVENTS,
        kind=SignalKind.EVENT,
        cluster=cluster,
        event_time=_event_time(ev),
        namespace=_namespace_of(ev),
        subject=Subject(
            kind=ev.involved_object.kind,
            name=ev.involved_object.name,
            uid=ev.involved_object.uid or None,
        ),
        owner=owner,
        node=node,
        # The cluster already classified this and its vocabulary is closed. Mirroring
        # it keeps the stored value faithful to what was observed; ranking beyond
        # that belongs in the assembler, where it can be revised without a
        # re-collection that this source cannot support.
        severity=Severity.WARNING if ev.type == WARNING_TYPE else Severity.INFO,
        dedupe_key=_dedupe_key(container, ev.reason, template.key),
        redacted=message_redacted,
        payload=partition_payload(payload, PROVENANCE_KEYS),
    )


# ---- the only I/O in this module -------------------------------------------


def collect(cluster: str) -> tuple[SourceStatus, list[Signal], str | None]:
    """Returns (status, signals, error).

    Pods, ReplicaSets and Jobs are listed here even though the pod collector lists
    them too. Sharing one snapshot between the two would couple their failure
    domains, and a per-source collection_runs row exists precisely so events can
    report ok while pod state reports unavailable. Four calls that do not grow with
    the cluster is the correct price for that independence.

    Any failure at all becomes unavailable with no signals, for the same reason
    list_all raises rather than truncating: a partial result the assembler cannot
    distinguish from a complete one reads as evidence of health.
    """
    try:
        events = k8s.list_all(k8s.core_v1().list_event_for_all_namespaces)
        pods = k8s.list_all(k8s.core_v1().list_pod_for_all_namespaces)
        replicasets = k8s.list_all(k8s.apps_v1().list_replica_set_for_all_namespaces)
        jobs = k8s.list_all(k8s.batch_v1().list_job_for_all_namespaces)
    except Exception as exc:
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"

    resolver = OwnerResolver.build(replicasets=replicasets, jobs=jobs)
    objects_by_uid = {
        obj.metadata.uid: obj for obj in (*pods, *replicasets, *jobs) if obj.metadata.uid
    }

    signals: list[Signal] = []
    for ev in events:
        # Cheapest predicate first: the allowlist drops most of the stream, and it is
        # a set membership test against fields already in hand.
        if not _should_keep(ev):
            continue
        if not k8s.in_scope(_namespace_of(ev)):
            continue
        signals.append(signal_for_event(ev, objects_by_uid, resolver, cluster))

    return (SourceStatus.OK if signals else SourceStatus.EMPTY), signals, None
