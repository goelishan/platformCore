"""
Resolve a pod to the workload that manages it.

  - Pure. Takes objects that have already been listed and returns a resolver, so it
    is testable with no cluster at all — which matters, because the cluster being
    broken is this agent's normal operating condition.
  - One ReplicaSet list plus one Job list replaces an API call per pod: 2 calls
    instead of 400 per cycle, and it does not grow with the cluster.
  - Keyed on uid, never name. Names get reused — delete a Deployment and recreate it
    and the new one would inherit the old one's recurrence history, poisoning
    first_seen_ever(), the one query where a false "we have seen this before" is
    worst.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from oncall.envelope import Owner

# Kinds that sit between a pod and its real workload. Anything else the controller
# reference points at — Deployment, StatefulSet, DaemonSet, CronJob, Node, some
# operator's CRD — is already the root and needs no hop.
INTERMEDIATE_KINDS = {"ReplicaSet", "Job"}


def controller_of(obj: Any) -> Any | None:
    """The ownerReference with controller=True.

    An object may carry several ownerReferences; exactly one is the controller that
    owns its lifecycle. Taking [0] works until it doesn't, and then it is wrong
    intermittently, which is the worst way to be wrong.
    """
    for ref in obj.metadata.owner_references or []:
        if ref.controller:
            return ref
    return None


class OwnerResolver:
    def __init__(self, roots: dict[str, Owner]) -> None:
        self._roots = roots

    @classmethod
    def build(cls, replicasets: Sequence[Any] = (), jobs: Sequence[Any] = ()) -> "OwnerResolver":
        """Map every intermediate object's uid to the root it rolls up to.

        Kinds are passed in rather than read off the objects: the API server omits
        `kind` on items inside a List response, so obj.kind is empty here even though
        it is populated on a single GET.
        """
        roots: dict[str, Owner] = {}

        for kind, objects in (("ReplicaSet", replicasets), ("Job", jobs)):
            for obj in objects:
                ctrl = controller_of(obj)
                roots[obj.metadata.uid] = (
                    Owner(kind=ctrl.kind, name=ctrl.name)
                    if ctrl
                    else Owner(kind=kind, name=obj.metadata.name)
                )

        return cls(roots)

    def resolve(self, pod: Any) -> tuple[Owner | None, bool]:
        """Returns (owner, resolved).

        resolved is False only when the pod points at an intermediate object that is
        not in the map — the ReplicaSet was garbage collected between the two list
        calls, which happens routinely during a rollout once revisionHistoryLimit is
        reached. The ReplicaSet reference is still returned, because it genuinely is
        the controller; we simply could not walk further.
        """
        ctrl = controller_of(pod)

        if ctrl is None:
            return None, True                                    # bare pod, truly unowned

        if ctrl.kind not in INTERMEDIATE_KINDS:
            return Owner(kind=ctrl.kind, name=ctrl.name), True    # DaemonSet, StatefulSet, Node

        root = self._roots.get(ctrl.uid)
        if root is None:
            return Owner(kind=ctrl.kind, name=ctrl.name), False   # RS gone — flag it

        return root, True