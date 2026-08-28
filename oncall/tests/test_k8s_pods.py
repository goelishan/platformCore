"""
Pod state mapping tests.

Both bugs this file guards against were found by looking at a real cluster twice,
thirty minutes apart — not by reasoning about the code. These tests pin the fixes so
the next reader cannot reintroduce either while the collector still looks correct in
a single snapshot.

Stubs only. The mapping never calls the API, so nothing here needs a cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncall.collectors.k8s_pods import signals_for_pod
from oncall.collectors.owners import OwnerResolver
from oncall.envelope import Severity, SignalKind, SignalSource, provenance_of

CLUSTER = "test-cluster"
T0 = datetime(2026, 8, 10, 13, 0, 0, tzinfo=UTC)
RESOLVER = OwnerResolver.build()


# ---- stubs -----------------------------------------------------------------


class _Bag:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def terminated(reason: str, exit_code: int, started: datetime, finished: datetime):
    return _Bag(
        reason=reason,
        exit_code=exit_code,
        started_at=started,
        finished_at=finished,
        message=None,
    )


def waiting(reason: str, message: str | None = None):
    return _Bag(reason=reason, message=message)


def running(started: datetime):
    return _Bag(started_at=started)


def container(
    name: str = "app",
    *,
    state_running=None,
    state_terminated=None,
    state_waiting=None,
    last_terminated=None,
    restart_count: int = 0,
    ready: bool = False,
    image: str = "busybox:1.36",
):
    return _Bag(
        name=name,
        restart_count=restart_count,
        ready=ready,
        image=image,
        state=_Bag(running=state_running, terminated=state_terminated, waiting=state_waiting),
        last_state=_Bag(running=None, terminated=last_terminated, waiting=None),
    )


def condition(type_: str, status: str, reason=None, message=None, at: datetime = T0):
    return _Bag(
        type=type_, status=status, reason=reason, message=message, last_transition_time=at
    )


def pod(
    name: str = "api-7f9",
    *,
    containers=(),
    conditions=(),
    phase: str = "Running",
    start_time: datetime | None = T0,
    node: str | None = "node-1",
    owner_refs=(),
):
    return _Bag(
        metadata=_Bag(
            name=name,
            namespace="prod",
            uid=f"uid-{name}",
            creation_timestamp=T0 - timedelta(minutes=5),
            owner_references=list(owner_refs),
        ),
        spec=_Bag(node_name=node),
        status=_Bag(
            phase=phase,
            start_time=start_time,
            conditions=list(conditions),
            container_statuses=list(containers) or None,
        ),
    )


def only(p):
    signals = signals_for_pod(p, RESOLVER, CLUSTER)
    assert len(signals) == 1
    return signals[0]


# ---- the backoff traps -----------------------------------------------------


def test_crashloopbackoff_and_terminated_are_the_same_signal():
    """The bug this file exists for.

    A crashing container alternates between two representations of one event: for a
    moment after exit the detail sits in state.terminated, then the kubelet starts
    backing off and it moves to last_state.terminated while state.waiting reports
    CrashLoopBackOff. Keying on the waiting reason would mint a second fingerprint
    for the same problem depending purely on when the poll landed, splitting one
    crashloop into two recurrence groups.
    """
    died_at = T0 + timedelta(seconds=2)
    term = terminated("Error", 1, T0, died_at)

    just_exited = only(pod(containers=[container(state_terminated=term, restart_count=3)]))
    backing_off = only(
        pod(
            containers=[
                container(
                    state_waiting=waiting("CrashLoopBackOff", "back-off 10s restarting..."),
                    last_terminated=term,
                    restart_count=3,
                )
            ]
        )
    )

    assert just_exited.dedupe_key == "app|Error|1|"
    assert backing_off.dedupe_key == "app|Error|1|"
    assert just_exited.signal_id == backing_off.signal_id


def test_backoff_state_is_kept_as_context_not_identity():
    backing_off = only(
        pod(
            containers=[
                container(
                    state_waiting=waiting("CrashLoopBackOff", "back-off 5m0s restarting..."),
                    last_terminated=terminated("OOMKilled", 137, T0, T0),
                    restart_count=9,
                )
            ]
        )
    )

    assert backing_off.dedupe_key == "app|OOMKilled|137|"
    assert backing_off.payload["waiting_reason"] == "CrashLoopBackOff"


def test_image_pull_names_collapse_to_one_key():
    """ErrImagePull and ImagePullBackOff are two phases of one failure, and the
    kubelet oscillates between them. Unlike a crashloop there is no termination
    behind either, so the names have to be reconciled directly."""
    attempt = only(pod(containers=[container(state_waiting=waiting("ErrImagePull", "no such host"))]))
    backoff = only(
        pod(containers=[container(state_waiting=waiting("ImagePullBackOff", "Back-off pulling"))])
    )

    assert attempt.dedupe_key == backoff.dedupe_key == "app|ErrImagePull||"
    assert attempt.fingerprint == backoff.fingerprint
    assert backoff.payload["waiting_reason"] == "ImagePullBackOff"


# ---- reason and exit code are only meaningful as a pair --------------------


def test_three_failures_that_share_a_field_stay_distinct():
    """137 is SIGKILL, used by the OOM killer and by the kubelet killing a container
    that failed its liveness probe. Neither reason nor exit code separates all three
    alone."""
    app_error = only(
        pod("a", containers=[container(state_terminated=terminated("Error", 1, T0, T0))])
    )
    oom = only(
        pod("a", containers=[container(state_terminated=terminated("OOMKilled", 137, T0, T0))])
    )
    killed = only(
        pod("a", containers=[container(state_terminated=terminated("Error", 137, T0, T0))])
    )

    keys = {app_error.dedupe_key, oom.dedupe_key, killed.dedupe_key}
    assert keys == {"app|Error|1|", "app|OOMKilled|137|", "app|Error|137|"}

    fingerprints = {app_error.fingerprint, oom.fingerprint, killed.fingerprint}
    assert len(fingerprints) == 3


# ---- a pod can look healthy and be the problem -----------------------------


def test_running_with_restarts_keys_on_the_last_termination():
    """Every health indicator on such a pod reads fine — running, ready, Ready
    condition True. The previous termination is the only evidence there is, and
    keying it as Running would make the most interesting pod invisible."""
    sig = only(
        pod(
            containers=[
                container(
                    state_running=running(T0 + timedelta(minutes=1)),
                    last_terminated=terminated("Error", 137, T0 - timedelta(seconds=50), T0),
                    restart_count=3,
                    ready=True,
                )
            ],
            conditions=[condition("Ready", "True")],
        )
    )

    assert sig.dedupe_key == "app|Error|137|"
    assert sig.severity == Severity.WARNING
    assert sig.event_time == T0


def test_healthy_container_is_stable_across_polls():
    """event_time comes from running.started_at, not the clock, so re-collecting an
    unchanged pod produces the same signal_id and the upsert holds it to one row.
    That is what makes emitting healthy pods affordable."""
    started = T0 - timedelta(days=6)
    first = only(pod(containers=[container(state_running=running(started), ready=True)]))
    later = only(pod(containers=[container(state_running=running(started), ready=True)]))

    assert first.dedupe_key == "app|Running||"
    assert first.severity == Severity.INFO
    assert first.signal_id == later.signal_id


# ---- the timestamp ladder --------------------------------------------------


def test_unschedulable_pod_still_gets_an_event_time():
    """No containerStatuses, no start_time, no container state — the only timestamp
    in the whole object is on the PodScheduled condition."""
    scheduled_at = T0 - timedelta(minutes=3)
    sig = only(
        pod(
            "pending-1",
            phase="Pending",
            start_time=None,
            node=None,
            conditions=[
                condition(
                    "PodScheduled",
                    "False",
                    reason="Unschedulable",
                    message="0/1 nodes are available: 1 Insufficient cpu.",
                    at=scheduled_at,
                )
            ],
        )
    )

    assert sig.event_time == scheduled_at
    assert sig.dedupe_key.startswith("|Unschedulable|")
    assert sig.kind == SignalKind.POD_STATE
    assert sig.source == SignalSource.K8S_PODS


def test_a_reconciled_reason_merges_its_causes_and_says_so():
    """A deliberate over-merge, pinned so nobody 'fixes' it into the bug underneath.

    ErrImagePull and ImagePullBackOff are two phases of one condition and each writes
    its own message, so no message-derived key can be stable across them — the
    fingerprint would depend on which phase a poll caught. Merging the causes is the
    lesser evil: a coarse identity loses structure, an unstable one invents it. The
    distinction survives in the payload for read time.
    """
    host = only(pod(containers=[container(state_waiting=waiting("ErrImagePull", "no such host"))]))
    manifest = only(
        pod(containers=[container(state_waiting=waiting("ErrImagePull", "manifest unknown"))])
    )

    assert host.fingerprint == manifest.fingerprint
    assert host.payload["message_template"] != manifest.payload["message_template"]
    assert "keyed_on_message" not in provenance_of(host.payload)


def test_a_standalone_reason_keys_on_its_message_and_says_so():
    """The other half of the same distinction: a template that is present and a
    template that is load-bearing are different facts, and the second cannot be
    inferred from the row without being written down."""
    sig = only(
        pod(containers=[container(state_waiting=waiting("CreateContainerConfigError", "no cm"))])
    )

    assert provenance_of(sig.payload)["keyed_on_message"] is True


def test_unschedulable_causes_do_not_share_a_fingerprint():
    """The reason on a PodScheduled condition is always 'Unschedulable', so before the
    message template entered the key an untolerated taint and insufficient CPU — two
    problems with two different fixes — read as one recurring problem with double the
    occurrences. The template separates them; the counts and node numbers it removes
    are what still lets each group with itself."""

    def unschedulable(message: str):
        return only(
            pod(
                "pending-1",
                phase="Pending",
                start_time=None,
                node=None,
                conditions=[
                    condition(
                        "PodScheduled", "False", reason="Unschedulable",
                        message=message, at=T0,
                    )
                ],
            )
        )

    cpu = unschedulable("0/3 nodes are available: 3 Insufficient cpu.")
    cpu_wider = unschedulable("0/5 nodes are available: 5 Insufficient cpu.")
    taint = unschedulable(
        "0/3 nodes are available: 3 node(s) had untolerated taint "
        "{node-role.kubernetes.io/control-plane: }."
    )

    assert cpu.fingerprint != taint.fingerprint
    assert cpu.fingerprint == cpu_wider.fingerprint


def test_waiting_container_falls_back_to_the_ready_condition():
    ready_at = T0 - timedelta(minutes=2)
    sig = only(
        pod(
            containers=[container(state_waiting=waiting("CreateContainerConfigError", "no cm"))],
            conditions=[condition("Ready", "False", reason="ContainersNotReady", at=ready_at)],
        )
    )

    assert sig.event_time == ready_at
    assert sig.dedupe_key.startswith("app|CreateContainerConfigError|")
    assert sig.payload["message"] == "no cm"


# ---- shape -----------------------------------------------------------------


def test_one_signal_per_container():
    """A pod where one container is healthy and one is crashlooping is two facts.
    A pod-level signal would blur them and lose the one that matters."""
    signals = signals_for_pod(
        pod(
            containers=[
                container("app", state_terminated=terminated("Error", 1, T0, T0), restart_count=4),
                container("sidecar", state_running=running(T0), ready=True),
            ]
        ),
        RESOLVER,
        CLUSTER,
    )

    assert len(signals) == 2
    assert {s.dedupe_key for s in signals} == {"app|Error|1|", "sidecar|Running||"}
    assert len({s.fingerprint for s in signals}) == 2
    # Same pod, so the subject is shared; the container lives in the key.
    assert {s.subject.name for s in signals} == {"api-7f9"}


def test_container_lifetime_is_recorded():
    """Dying in 0s points at startup configuration; running 50s and then dying points
    at something external arriving later. Context, not identity."""
    instant = only(
        pod(containers=[container(state_terminated=terminated("OOMKilled", 137, T0, T0))])
    )
    lingering = only(
        pod(
            containers=[
                container(
                    state_terminated=terminated("Error", 137, T0, T0 + timedelta(seconds=50))
                )
            ]
        )
    )

    assert instant.payload["container_lifetime_seconds"] == 0
    assert lingering.payload["container_lifetime_seconds"] == 50


def test_unresolved_owner_is_marked_in_payload():
    """A ReplicaSet garbage collected mid-rollout. The marker stops the assembler
    reading a lookup failure as a pod that genuinely has no owner."""
    ref = _Bag(kind="ReplicaSet", name="api-7f9d4c", uid="rs-gone", controller=True)
    sig = only(
        pod(
            owner_refs=[ref],
            containers=[container(state_running=running(T0), ready=True)],
        )
    )

    assert sig.owner.kind == "ReplicaSet"
    assert sig.payload["owner_resolution"] == "partial"


def test_node_is_populated_but_not_part_of_identity():
    args = dict(containers=[container(state_terminated=terminated("Error", 1, T0, T0))])
    here = only(pod(node="node-1", **args))
    there = only(pod(node="node-9", **args))

    assert here.node == "node-1"
    assert there.node == "node-9"
    assert here.fingerprint == there.fingerprint
    assert here.signal_id == there.signal_id
