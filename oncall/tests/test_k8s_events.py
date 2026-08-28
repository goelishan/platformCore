"""
Event mapping tests.

An event is a stream, not state: the API server drops it at --event-ttl and it can
never be re-read. Two consequences drive most of what is pinned here — the write
path has to stay faithful because there is no second chance to revise it, and the
object arrives already collapsed by the API server's own write-time aggregation,
so what event_time is bound to decides whether recurrence survives at all.

Stubs only. The mapping never calls the API, so nothing here needs a cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncall.collectors.k8s_events import (
    _container_from_field_path,
    _event_time,
    _should_keep,
    signal_for_event,
)
from oncall.collectors.owners import OwnerResolver
from oncall.envelope import Severity, SignalKind, SignalSource, provenance_of

CLUSTER = "test-cluster"
T0 = datetime(2026, 8, 12, 3, 14, 0, tzinfo=UTC)


# ---- stubs -----------------------------------------------------------------


class _Bag:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# None is a meaningful value for every timestamp on an event — it is the whole reason
# the ladder exists — so "not supplied" needs a value of its own.
_UNSET = object()


def involved(
    kind: str = "Pod",
    name: str = "api-7f9-x2k",
    *,
    namespace: str | None = "prod",
    uid: str | None = "uid-api-7f9-x2k",
    field_path: str | None = "spec.containers{app}",
):
    return _Bag(
        kind=kind, name=name, namespace=namespace, uid=uid, field_path=field_path
    )


def event(
    *,
    type_: str = "Warning",
    reason: str = "BackOff",
    message: str | None = "Back-off restarting failed container",
    count: int | None = 7,
    first=_UNSET,
    last: datetime | None = T0,
    event_time: datetime | None = None,
    series=None,
    component: str | None = "kubelet",
    obj=None,
    created: datetime = T0 - timedelta(hours=1),
    uid: str = "uid-ev-1",
):
    return _Bag(
        type=type_,
        reason=reason,
        message=message,
        count=count,
        first_timestamp=(T0 - timedelta(minutes=10)) if first is _UNSET else first,
        last_timestamp=last,
        event_time=event_time,
        series=series,
        source=_Bag(component=component),
        reporting_component="",
        # Deliberately 'default' while the subject lives in 'prod'. Node events really
        # are stored this way, and no test here should ever see this value surface.
        metadata=_Bag(namespace="default", creation_timestamp=created, uid=uid),
        involved_object=obj if obj is not None else involved(),
    )


def replicaset(uid: str = "uid-rs", name: str = "api-7f9", owner: str | None = "api"):
    refs = [_Bag(controller=True, kind="Deployment", name=owner, uid="uid-dep")] if owner else []
    return _Bag(metadata=_Bag(uid=uid, name=name, owner_references=refs))


def pod(
    uid: str = "uid-api-7f9-x2k",
    name: str = "api-7f9-x2k",
    *,
    node: str | None = "node-1",
    rs_uid: str | None = "uid-rs",
):
    refs = (
        [_Bag(controller=True, kind="ReplicaSet", name="api-7f9", uid=rs_uid)]
        if rs_uid
        else []
    )
    return _Bag(
        metadata=_Bag(uid=uid, name=name, namespace="prod", owner_references=refs),
        spec=_Bag(node_name=node),
    )


RESOLVER = OwnerResolver.build(replicasets=[replicaset()])
POD_MAP = {"uid-api-7f9-x2k": pod()}


def sig(ev, objects=None, resolver=RESOLVER):
    return signal_for_event(ev, POD_MAP if objects is None else objects, resolver, CLUSTER)


# ---- the aggregation trap --------------------------------------------------
# The API server collapses recurrence on write: same object, same reason, and it
# mutates one row rather than adding another. count goes up, lastTimestamp moves,
# every intermediate timestamp is destroyed. Binding event_time to lastTimestamp is
# what stops the loss there — an unchanged event collapses, a new occurrence does
# not — and is the whole reason this source is polled every 30s.


def test_unchanged_event_repolled_is_one_row():
    """Re-observing the same event must not mint a second row. If it did, a quiet
    event sitting in the API server for its full hour would write 120 rows and
    inflate every recurrence count that reads it."""
    assert sig(event()).signal_id == sig(event()).signal_id


def test_new_occurrence_is_a_new_row_in_the_same_group():
    """lastTimestamp advancing is the only evidence of a fresh occurrence available,
    and it has to produce a row — collapsed at read time by fingerprint, never here."""
    before = sig(event(last=T0, count=7))
    after = sig(event(last=T0 + timedelta(seconds=40), count=8))

    assert after.signal_id != before.signal_id
    assert after.fingerprint == before.fingerprint


def test_count_is_recorded_absolute_never_as_a_delta():
    """The collector cannot compute a delta without reading the landing zone, which
    the layering forbids. Storing the absolute lets the reader diff consecutive rows
    and learn that occurrences landed inside a window we did not sample — the
    under-sampling stays visible in the data instead of silently disappearing."""
    assert sig(event(count=7)).payload["count"] == 7
    assert sig(event(count=None)).payload.get("count") is None


def test_count_is_read_from_series_when_the_legacy_field_is_empty():
    ev = event(count=None, series=_Bag(count=12, last_observed_time=T0))
    assert sig(ev).payload["count"] == 12


def test_count_carries_the_identity_of_the_object_that_counted_it():
    """Found on a real cluster, not in review: a node reboot at 05:35 left two live
    Event objects for one ongoing problem, counts 38 and 6, both mapping here to one
    fingerprint. Aggregation is client-side and cached in the kubelet's memory, so a
    restart abandons the old object and starts a second from one.

    Without event_uid the reader sees a count sequence of 38 then 6 for a single
    group and cannot tell a reset from a decrease from two interleaved counters.
    """
    assert provenance_of(sig(event(uid="uid-ev-1")).payload)["event_uid"] == "uid-ev-1"


def test_two_generations_of_one_problem_share_a_fingerprint():
    """The other half of the same finding, and the half that held. Identity is derived
    from content, never from the API object, so the recurrence group survived the
    reboot intact. Keying on event_uid would have split one ongoing problem in two."""
    before = sig(event(uid="uid-ev-old", last=T0 - timedelta(hours=18), count=38))
    after = sig(event(uid="uid-ev-new", last=T0, count=6))

    assert before.fingerprint == after.fingerprint
    assert before.signal_id != after.signal_id
    assert provenance_of(before.payload)["event_uid"] != provenance_of(after.payload)["event_uid"]


def test_a_fossil_event_collapses_to_one_row_however_often_it_is_recollected():
    """An abandoned object keeps being listed and its lastTimestamp never moves again.
    Because event_time is bound to that field, every poll reproduces the same
    signal_id and the stale object costs exactly one row rather than one per cycle."""
    fossil = event(uid="uid-ev-old", last=T0 - timedelta(hours=18), count=38)
    assert sig(fossil).signal_id == sig(fossil).signal_id


# ---- the timestamp ladder --------------------------------------------------
# Two generations of the events API write into the same storage, so a single list
# call returns a mixture: events.k8s.io/v1 writers fill eventTime and a series and
# leave the deprecated pair null. Unlike the pod collector's ladder, this one is the
# normal path, not the exceptional one.


def test_series_last_observed_time_wins():
    """For an aggregating event this is the truest statement of when the problem last
    occurred, so it outranks the deprecated field even when both are present."""
    ev = event(last=T0 - timedelta(minutes=5), series=_Bag(count=3, last_observed_time=T0))
    assert _event_time(ev) == T0


def test_ladder_falls_through_to_event_time():
    ev = event(last=None, event_time=T0, first=T0 - timedelta(minutes=30))
    assert _event_time(ev) == T0


def test_ladder_floor_is_creation_not_the_clock():
    """The floor exists so the ladder can never reach for the clock. Doing so would
    set event_time equal to collected_at, collapsing the two timestamps the envelope
    keeps apart, and would hand every poll a fresh signal_id."""
    created = T0 - timedelta(hours=2)
    ev = event(last=None, event_time=None, first=None, created=created)
    assert _event_time(ev) == created


# ---- filtering -------------------------------------------------------------
# Asymmetric on purpose. Warning is an open set because an unfamiliar Warning is the
# most interesting row in the table; Normal is deny-by-default because an unfamiliar
# Normal is almost certainly routine lifecycle.


def test_unknown_warning_is_kept():
    assert _should_keep(event(type_="Warning", reason="SomeOperatorInventedThis"))


def test_routine_lifecycle_normals_are_dropped():
    """The kubelet emits these on every container start, so a crashlooping pod floods
    the stream with them — and pod state already reports the same fact more precisely."""
    for reason in ("Pulling", "Pulled", "Created", "Started"):
        assert not _should_keep(event(type_="Normal", reason=reason))


def test_allowlisted_normals_are_kept():
    """Warnings alone are uninterpretable. 'OOMKilled at 03:14' is a fact; the
    ScalingReplicaSet two minutes earlier is what turns it into a diagnosis."""
    for reason in ("Scheduled", "Killing", "ScalingReplicaSet", "NodeNotReady"):
        assert _should_keep(event(type_="Normal", reason=reason))


# ---- severity --------------------------------------------------------------


def test_severity_mirrors_the_events_own_type():
    """No curated escalation list. Reason strings are not a stable API — operators mint
    their own — so a hardcoded list would read a novel failure as less serious than a
    familiar one, which is the wrong bias for a triage tool. Ranking belongs in the
    assembler, where it can be revised without a re-collection this source cannot
    support.
    """
    assert sig(event(type_="Warning", reason="FailedScheduling")).severity is Severity.WARNING
    assert sig(event(type_="Warning", reason="Unhealthy")).severity is Severity.WARNING
    assert sig(event(type_="Normal", reason="Scheduled")).severity is Severity.INFO


def test_type_and_reason_survive_verbatim_in_the_payload():
    s = sig(event(type_="Warning", reason="FailedMount"))
    assert s.payload["type"] == "Warning"
    assert s.payload["reason"] == "FailedMount"


# ---- identity --------------------------------------------------------------


def test_two_containers_sharing_a_reason_stay_distinct():
    """Mirrors the pod collector's one-signal-per-container rule: two containers in one
    pod failing their probes are two problems that happen to share a reason."""
    app = sig(event(obj=involved(field_path="spec.containers{app}")))
    car = sig(event(obj=involved(field_path="spec.containers{sidecar}")))

    assert app.dedupe_key.startswith("app|BackOff|")
    assert car.dedupe_key.startswith("sidecar|BackOff|")
    assert app.fingerprint != car.fingerprint


def test_volatile_tokens_in_a_message_do_not_split_one_problem():
    """The raw message carries back-off intervals, addresses and counts, so keying on
    it would make every occurrence unique and every recurrence group size one. The
    template removes exactly those tokens, so the two halves of a widening backoff stay
    one problem — and the unmasked text still travels in the payload."""
    a = sig(event(message="back-off 10s restarting failed container"))
    b = sig(event(message="back-off 5m0s restarting failed container"))

    assert a.fingerprint == b.fingerprint
    assert a.payload["message"] != b.payload["message"]


def test_conditions_sharing_a_reason_are_separated_by_their_message():
    """'Unhealthy' covers a probe that timed out and a probe that could not resolve its
    host. Same reason, same container, different fixes — before the template entered
    the key they were one fingerprint and the assembler ranked them as one problem."""
    timeout = sig(
        event(reason="Unhealthy", message='Liveness probe failed: Get "http://10.244.0.7:8080/healthz": context deadline exceeded')
    )
    dns = sig(
        event(reason="Unhealthy", message='Liveness probe failed: Get "http://10.244.0.7:8080/healthz": no such host')
    )
    rescheduled = sig(
        event(reason="Unhealthy", message='Liveness probe failed: Get "http://10.244.3.19:8080/healthz": context deadline exceeded')
    )

    assert timeout.fingerprint != dns.fingerprint
    assert timeout.fingerprint == rescheduled.fingerprint


def test_component_is_provenance_not_identity():
    """Who reported a condition does not change what the condition is — the same
    argument that keeps node out of the fingerprint. Including it would split one
    problem in two the moment a different component reported it."""
    a = sig(event(component="kubelet"))
    b = sig(event(component="default-scheduler"))

    assert a.fingerprint == b.fingerprint
    assert provenance_of(a.payload)["component"] == "kubelet"
    assert provenance_of(b.payload)["component"] == "default-scheduler"


def test_field_path_parsing():
    assert _container_from_field_path("spec.containers{app}") == "app"
    assert _container_from_field_path("spec.initContainers{wait-for-db}") == "wait-for-db"
    assert _container_from_field_path(None) == ""
    assert _container_from_field_path("spec.containers") == ""


def test_pod_level_event_has_no_container_in_its_key():
    s = sig(event(reason="FailedScheduling", obj=involved(field_path=None)))
    assert s.dedupe_key.startswith("|FailedScheduling|")
    assert "container" not in s.payload


# ---- owner resolution ------------------------------------------------------
# involvedObject is a reference, not an object, so there is no ownerReferences chain
# to walk. Owner is not cosmetic: idx_sig_owner and signals_in_window(owner_name=...)
# are how a pod crash correlates with the rollout that caused it, and a signal
# without one cannot participate in that join at all.


def test_pod_event_rolls_up_to_the_deployment():
    s = sig(event())
    assert s.owner is not None
    assert (s.owner.kind, s.owner.name) == ("Deployment", "api")
    assert "owner_resolution" not in s.payload


def test_deleted_subject_is_marked_not_silently_unowned():
    """The highest-value case in this source, not an error: the most useful event in an
    incident is often the one describing a pod that no longer exists. Resolving it by
    parsing the owner out of the pod name is refused for the reason owners.py keys on
    uid — names get reused, and inheriting a deleted workload's history is the one way
    first_seen_ever() can lie."""
    s = sig(event(), objects={})

    assert s.owner is None
    assert s.payload["owner_resolution"] == "subject_gone"


def test_garbage_collected_replicaset_is_partial_not_gone():
    """Distinct from subject_gone: the pod is right here, we simply could not walk past
    a ReplicaSet that was collected mid-rollout. The intermediate is still returned,
    because it genuinely is the controller."""
    orphan = pod(rs_uid="uid-rs-vanished")
    s = sig(event(), objects={"uid-api-7f9-x2k": orphan})

    assert s.owner is not None
    assert (s.owner.kind, s.owner.name) == ("ReplicaSet", "api-7f9")
    assert s.payload["owner_resolution"] == "partial"


def test_missing_subject_uid_is_its_own_marker():
    """An event that names no subject uid cannot be looked up at all, which is a
    different fact from a subject that was deleted. Four absences, four markers —
    the assembler must never read one as another."""
    s = sig(event(obj=involved(uid=None)))

    assert s.owner is None
    assert s.payload["owner_resolution"] == "no_subject_uid"


def test_bare_pod_is_unowned_with_no_marker():
    """Genuinely unowned, and correctly silent about it. This is the case a blanket
    'owner is None' would be confused with."""
    s = sig(event(), objects={"uid-api-7f9-x2k": pod(rs_uid=None)})

    assert s.owner is None
    assert "owner_resolution" not in s.payload


def test_root_kind_subject_is_its_own_owner():
    """Redundant-looking, and it is what makes the owner join uniform: a
    ScalingReplicaSet event on Deployment/api and a crash on a pod beneath it then
    share owner_name, so correlating them is one GROUP BY."""
    s = sig(event(type_="Normal", reason="ScalingReplicaSet",
                  obj=involved(kind="Deployment", name="api", field_path=None)))

    assert s.owner is not None
    assert (s.owner.kind, s.owner.name) == ("Deployment", "api")


def test_replicaset_subject_hops_to_its_deployment():
    s = sig(
        event(type_="Normal", reason="SuccessfulCreate",
              obj=involved(kind="ReplicaSet", name="api-7f9", uid="uid-rs", field_path=None)),
        objects={"uid-rs": replicaset()},
    )

    assert s.owner is not None
    assert (s.owner.kind, s.owner.name) == ("Deployment", "api")


# ---- location and scope ----------------------------------------------------


def test_pod_events_carry_the_node_they_ran_on():
    """Lets an event join the node-scoped index, which is what separates 'this workload
    is broken' from 'everything on this node is broken'."""
    assert sig(event()).node == "node-1"


def test_node_event_is_cluster_scoped_and_names_the_node():
    """The trap this pins: a Node event has an empty involvedObject.namespace but is
    itself stored in 'default'. Reading the event's own namespace would drop every node
    event behind an allowlist that omits 'default' — and node events are what explain a
    cluster-wide incident. Storing it would assert a Node lives in a namespace, which
    is false and corrupts every namespace-filtered query."""
    s = sig(
        event(reason="NodeNotReady",
              obj=involved(kind="Node", name="node-1", namespace="", uid="uid-node-1",
                           field_path=None)),
        objects={},
    )

    assert s.namespace is None
    assert s.node == "node-1"
    assert s.payload.get("owner_resolution") is None


def test_namespace_comes_from_the_subject_not_the_event():
    assert sig(event()).namespace == "prod"


# ---- envelope conformance --------------------------------------------------


def test_source_and_kind_are_pinned():
    s = sig(event())
    assert s.source is SignalSource.K8S_EVENTS
    assert s.kind is SignalKind.EVENT


def test_long_message_is_truncated_and_says_so():
    """An oversized row costs storage on every poll, while an event message is
    front-loaded. Silent truncation would be worse than none — the flag is what stops
    the reasoner treating a cut-off message as a complete one."""
    s = sig(event(message="x" * 5000))

    assert len(s.payload["message"]) == 1024
    assert s.payload["message_truncated"] is True


def test_short_message_carries_no_truncation_flag():
    assert "message_truncated" not in sig(event(message="short")).payload
