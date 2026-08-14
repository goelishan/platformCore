"""
Pod state collector.

  - The mapping is pure. It takes pods that have already been listed and returns
    Signals, so every rule below is exercisable against a stub with no cluster.
    collect() is the only function here that performs I/O.
  - One signal per container, not per pod. A two-container pod with one healthy and
    one crashlooping is two facts, and blurring them loses the one that matters.
  - event_time always comes from the object, never from the clock. An unchanged pod
    therefore produces the same signal_id on every poll and collapses to a single
    row, which is what makes emitting healthy pods affordable at all.
  - status.phase decides nothing. A pod that has crashed five times still reports
    Running; the truth lives in containerStatuses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from oncall.collectors import k8s_client as k8s
from oncall.collectors.owners import OwnerResolver
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    iso,
    redact,
    template_of,
)

RUNNING_REASON = "Running"

# Waiting reasons that describe the kubelet's restart scheduling rather than any
# failure. The cause always lives in last_state.terminated; keying on these would
# name the symptom and discard the diagnosis.
BACKOFF_REASONS = {"CrashLoopBackOff"}

# Backoff states with no termination behind them, because the container never ran.
# The kubelet alternates between the attempt and the backoff — ErrImagePull, then
# ImagePullBackOff, then ErrImagePull — so polling at different moments in that
# cycle would otherwise split one failure across two fingerprints. Key on the
# attempt, keep the backoff name as context in the payload.
BACKOFF_ALIASES = {"ImagePullBackOff": "ErrImagePull"}

# Both sides of every aliased pair. Reconciliation is a property of the pair, not of
# the name that happened to get rewritten, so anything keyed off "was this aliased"
# has to test membership here. Applying it to the renamed half alone leaves the two
# phases keyed differently through whatever else varies between them — the same split
# the alias exists to prevent, surviving in a form that is harder to see.
RECONCILED_REASONS = set(BACKOFF_ALIASES) | set(BACKOFF_ALIASES.values())


# ---- timestamp ladder ------------------------------------------------------
# Waiting states and unschedulable pods carry no timestamp of their own. Falling
# back to the clock would make event_time equal collected_at, collapsing the two
# timestamps M1 deliberately keeps apart, and would hand every poll a fresh
# signal_id — one new row per minute for precisely the pods that are stuck.
#
# The ladder ends at creationTimestamp, which every object always has, so the
# bottom rung is never reached in practice.


def _condition(pod: Any, type_name: str) -> Any | None:
    for cond in pod.status.conditions or []:
        if cond.type == type_name:
            return cond
    return None


def _fallback_time(pod: Any) -> datetime:
    for cond_type in ("Ready", "PodScheduled"):
        cond = _condition(pod, cond_type)
        if cond and cond.last_transition_time:
            return cond.last_transition_time

    return pod.status.start_time or pod.metadata.creation_timestamp


# ---- what varies by container state ----------------------------------------
# reason and exit_code are returned as a pair because neither identifies a failure
# alone. 137 is SIGKILL, used both by the OOM killer and by the kubelet restarting
# a container that failed its liveness probe:
#
#   Error     + 1    the application exited on its own
#   OOMKilled + 137  the kernel killed it for memory
#   Error     + 137  something external killed a healthy process


def _from_terminated(term: Any, severity: Severity) -> dict[str, Any]:
    return {
        "event_time": term.finished_at,
        "reason": term.reason,
        "exit_code": term.exit_code,
        "started_at": term.started_at,
        "finished_at": term.finished_at,
        "severity": severity,
    }


def _container_facts(pod: Any, cs: Any) -> dict[str, Any]:
    state = cs.state
    last = cs.last_state.terminated if cs.last_state else None

    # A crashing container alternates between two representations of the same event.
    # For a moment after it exits the detail sits in state.terminated; once the
    # kubelet starts backing off it moves to last_state.terminated and state.waiting
    # reports CrashLoopBackOff. Reading the waiting reason there would key the signal
    # on a scheduling state rather than a cause, merging an application error, an
    # OOM kill and a liveness kill into one group — and would mint a second
    # fingerprint for the same problem depending on when the poll happened to land.
    if state.terminated:
        term = state.terminated
        return _from_terminated(
            term, Severity.INFO if term.exit_code == 0 else Severity.ERROR
        )

    if state.waiting:
        waiting = state.waiting

        if waiting.reason in BACKOFF_REASONS and last:
            facts = _from_terminated(last, Severity.ERROR)
            # Kept as context, deliberately not as key_message. This text describes the
            # kubelet's backoff — "back-off 5m0s restarting failed container" — not the
            # cause, which is the termination this branch already keyed on. Letting it
            # into identity would split one crashloop across the two representations
            # the branch exists to reconcile, since state.terminated carries no message
            # of its own and the fingerprint would flip every time the poll landed on
            # the other side.
            facts["message"] = waiting.message
            facts["waiting_reason"] = waiting.reason
            return facts

        # The container never ran, so there is no termination to read and the waiting
        # reason is the whole story. Aliased first, because a pull failure oscillates
        # between two names for one condition.
        reason = BACKOFF_ALIASES.get(waiting.reason, waiting.reason)
        reconciled = waiting.reason in RECONCILED_REASONS
        return {
            "event_time": _fallback_time(pod),
            "reason": reason,
            "exit_code": None,
            "message": waiting.message,
            # Identity-bearing only when the reason stands on its own. A reconciled
            # pair oscillates between two phases of one condition and each phase writes
            # its own message — "no such host" while pulling, "Back-off pulling image"
            # while waiting — so a message-derived key cannot be stable across them and
            # the fingerprint would depend on which phase the poll happened to catch.
            #
            # The cost is a genuine over-merge: a missing host and an unknown manifest
            # are different causes of a pull failure and now share a fingerprint. Taken
            # knowingly, because an identity that flips with poll timing is worse than
            # a coarse one — it does not lose structure, it invents it. The message and
            # its template still travel in the payload, so the distinction survives at
            # read time, which is where it can be revised.
            "key_message": None if reconciled else waiting.message,
            "waiting_reason": waiting.reason if reason != waiting.reason else None,
            "severity": Severity.ERROR,
        }

    if cs.restart_count and last:
        # Running now, but it was killed before. Every other indicator on such a pod
        # reads healthy — ready True, Ready condition True — so the previous
        # termination is the only evidence that anything is wrong.
        return _from_terminated(last, Severity.WARNING)

    return {
        "event_time": state.running.started_at if state.running else _fallback_time(pod),
        "reason": RUNNING_REASON,
        "exit_code": None,
        "severity": Severity.INFO,
    }


def _pod_level_facts(pod: Any) -> dict[str, Any]:
    """No containerStatuses at all — the pod never got far enough to have any.
    Unschedulable is the usual cause, and its only evidence is a PodScheduled
    condition with status False and a reason on the condition rather than on a
    container."""
    for cond in pod.status.conditions or []:
        if cond.status == "False" and cond.reason:
            return {
                "event_time": cond.last_transition_time,
                "reason": cond.reason,
                "message": cond.message,
                # The one place the message is the whole diagnosis. Every unschedulable
                # pod in the cluster carries reason 'Unschedulable' whatever the cause,
                # so without the message an untolerated taint and insufficient CPU are
                # one fingerprint.
                "key_message": cond.message,
                "exit_code": None,
                "severity": Severity.ERROR,
            }

    return {
        "event_time": _fallback_time(pod),
        "reason": pod.status.phase or "Unknown",
        "exit_code": None,
        "severity": Severity.WARNING,
    }


# ---- signal construction ---------------------------------------------------


def _dedupe_key(
    container: str,
    reason: str | None,
    exit_code: int | None,
    message_key: str,
) -> str:
    """container|reason|exit_code|message_template. Every part is stable across
    occurrences of the same failure. A restart counter must never appear here: it
    increments, so each restart would form its own recurrence group and the spacing
    analysis would see N problems of one occurrence rather than one problem occurring
    N times.

    The message template is the last part and the newest. Without it every
    unschedulable pod in the cluster keyed as |Unschedulable| regardless of cause, so
    an untolerated taint and insufficient CPU — two problems with two different fixes —
    shared a fingerprint and were reported as one recurring problem. The raw message
    could not go in for the opposite reason: it carries counts and addresses, so every
    occurrence would have been unique. The template is the stable middle.
    """
    return (
        f"{container}|{reason or ''}|"
        f"{'' if exit_code is None else exit_code}|{message_key}"
    )


def _build(
    pod: Any,
    cs: Any | None,
    facts: dict[str, Any],
    owner: Any,
    owner_resolved: bool,
    cluster: str,
) -> Signal:
    container = cs.name if cs else ""
    exit_code = facts.get("exit_code")

    # Redaction happens before anything is stored, not before the prompt: this row is
    # about to be written to a file on a volume and shipped to Postgres, so stripping
    # a credential later would still have persisted it twice. Kubernetes messages are
    # not a common place for secrets, but they quote whatever a controller was handed.
    message, message_redacted = redact(facts.get("message"))

    # Two different questions, so two different values. Everything observed is stored;
    # only what describes the same condition the reason names reaches the key. Where a
    # reason was reconciled across representations — a crashloop's two states, a pull
    # failure's two phases — the message follows the representation rather than the
    # condition, and keying on it would re-split exactly what the reconciliation joined.
    key_message, _ = redact(facts.get("key_message"))
    template = template_of(key_message)

    # The payload's template comes from the full message, not from key_message, so a
    # reader can still separate causes the key had to merge. keyed_on_message says
    # which of the two happened, because a template that is present and a template that
    # is load-bearing are different facts and one of them cannot be inferred.
    shown = template_of(message)

    # Fields are chosen explicitly rather than dumping the object. A serialised pod
    # carries managedFields and the full spec, none of which helps a diagnosis and
    # all of which costs prompt budget in M5.
    payload: dict[str, Any] = {
        "phase": pod.status.phase,
        "reason": facts.get("reason"),
        "container": container or None,
        "restart_count": cs.restart_count if cs else None,
        "ready": cs.ready if cs else None,
        "image": cs.image if cs else None,
        "exit_code": exit_code,
        "message": message or None,
        # The masked form. Readable, so a recurrence group can be explained without
        # re-deriving anything, and it costs a line of text rather than a hash nobody
        # can interpret.
        "message_template": shown.text or None,
        "keyed_on_message": bool(template.key) or None,
        # The current scheduling state, kept out of the key but useful context: the
        # LLM should know a container is in backoff, not only why it died.
        "waiting_reason": facts.get("waiting_reason"),
        "pod_start_time": iso(pod.status.start_time) if pod.status.start_time else None,
    }

    started, finished = facts.get("started_at"), facts.get("finished_at")
    if started and finished:
        # How long the container survived. Dying in 0s points at startup config;
        # running 50s and then dying points at something external arriving later.
        payload["container_lifetime_seconds"] = int((finished - started).total_seconds())

    if not owner_resolved:
        # The ReplicaSet was garbage collected before we listed it. Recorded so the
        # assembler cannot read a lookup failure as a pod that has no owner.
        payload["owner_resolution"] = "partial"

    return Signal(
        source=SignalSource.K8S_PODS,
        kind=SignalKind.POD_STATE,
        cluster=cluster,
        event_time=facts["event_time"],
        namespace=pod.metadata.namespace,
        subject=Subject(kind="Pod", name=pod.metadata.name, uid=pod.metadata.uid),
        owner=owner,
        node=pod.spec.node_name,
        severity=facts["severity"],
        dedupe_key=_dedupe_key(container, facts.get("reason"), exit_code, template.key),
        redacted=message_redacted,
        payload={k: v for k, v in payload.items() if v is not None},
    )


def signals_for_pod(pod: Any, resolver: OwnerResolver, cluster: str) -> list[Signal]:
    owner, resolved = resolver.resolve(pod)
    statuses = pod.status.container_statuses or []

    if not statuses:
        return [_build(pod, None, _pod_level_facts(pod), owner, resolved, cluster)]

    return [
        _build(pod, cs, _container_facts(pod, cs), owner, resolved, cluster)
        for cs in statuses
    ]


# ---- the only I/O in this module -------------------------------------------


def collect(cluster: str) -> tuple[SourceStatus, list[Signal], str | None]:
    """Returns (status, signals, error).

    Any failure at all becomes unavailable with no signals. Returning whatever was
    collected before the error would be a partial snapshot presented as a complete
    one, which the assembler has no way to detect — the same reason list_all raises
    rather than truncating.
    """
    try:
        pods = k8s.list_all(k8s.core_v1().list_pod_for_all_namespaces)
        replicasets = k8s.list_all(k8s.apps_v1().list_replica_set_for_all_namespaces)
        jobs = k8s.list_all(k8s.batch_v1().list_job_for_all_namespaces)
    except Exception as exc:
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"

    resolver = OwnerResolver.build(replicasets=replicasets, jobs=jobs)

    signals: list[Signal] = []
    for pod in pods:
        if not k8s.in_scope(pod.metadata.namespace):
            continue
        signals.extend(signals_for_pod(pod, resolver, cluster))

    return (SourceStatus.OK if signals else SourceStatus.EMPTY), signals, None
