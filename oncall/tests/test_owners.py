"""
Owner resolution tests.

Runs against hand-built stubs rather than a cluster, which is the point: the agent's
normal operating condition is a cluster that is broken or unreachable, so the logic
that reasons about it must be exercisable without one.

A stub needs only .metadata.uid, .metadata.name and .metadata.owner_references —
that is the entire surface OwnerResolver touches.
"""

from __future__ import annotations

from oncall.collectors.owners import OwnerResolver, controller_of
from oncall.envelope import Owner


class Ref:
    def __init__(self, kind: str, name: str, uid: str = "", controller: bool = True):
        self.kind = kind
        self.name = name
        self.uid = uid
        self.controller = controller


class Meta:
    def __init__(self, uid: str, name: str, owner_references: list[Ref] | None = None):
        self.uid = uid
        self.name = name
        self.owner_references = owner_references or []


class Obj:
    def __init__(self, uid: str, name: str, owner_references: list[Ref] | None = None):
        self.metadata = Meta(uid, name, owner_references)


# ---- controller_of ---------------------------------------------------------


def test_picks_the_controller_not_the_first_reference():
    """Objects can carry several ownerReferences; exactly one owns the lifecycle.
    Taking [0] works until it doesn't, and then it is wrong intermittently."""
    pod = Obj(
        "pod-1",
        "api-xk2mq",
        [
            Ref("SomethingElse", "decoy", "decoy-uid", controller=False),
            Ref("ReplicaSet", "api-7f9d4c", "rs-1", controller=True),
        ],
    )

    ctrl = controller_of(pod)

    assert ctrl.kind == "ReplicaSet"
    assert ctrl.name == "api-7f9d4c"


def test_no_controller_reads_as_unowned():
    pod = Obj("pod-1", "api", [Ref("Thing", "x", "u", controller=False)])

    assert controller_of(pod) is None


# ---- the three shapes seen in a real cluster -------------------------------


def test_deployment_resolves_through_its_replicaset():
    rs = Obj("rs-1", "coredns-7d764666f9", [Ref("Deployment", "coredns", "dep-1")])
    pod = Obj("pod-1", "coredns-7d764666f9-2hpvf", [Ref("ReplicaSet", "coredns-7d764666f9", "rs-1")])

    owner, resolved = OwnerResolver.build(replicasets=[rs]).resolve(pod)

    assert owner == Owner(kind="Deployment", name="coredns")
    assert resolved is True


def test_daemonset_needs_no_hop():
    pod = Obj("pod-1", "kube-proxy-vxk5l", [Ref("DaemonSet", "kube-proxy", "ds-1")])

    owner, resolved = OwnerResolver.build().resolve(pod)

    assert owner == Owner(kind="DaemonSet", name="kube-proxy")
    assert resolved is True


def test_static_pod_keeps_its_node_owner():
    """kubelet-managed pods are owned by the Node. Recorded faithfully rather than
    nulled: None would conflate a static pod with a bare pod and with a lookup
    failure. owner_kind carries the meaning, and downstream branches on it."""
    pod = Obj("pod-1", "kube-apiserver-node-1", [Ref("Node", "node-1", "node-uid")])

    owner, resolved = OwnerResolver.build().resolve(pod)

    assert owner == Owner(kind="Node", name="node-1")
    assert resolved is True


def test_bare_pod_has_no_owner():
    pod = Obj("pod-1", "debug-shell")

    owner, resolved = OwnerResolver.build().resolve(pod)

    assert owner is None
    assert resolved is True


# ---- the failure modes -----------------------------------------------------


def test_garbage_collected_replicaset_is_flagged_not_dropped():
    """Old ReplicaSets are GC'd once revisionHistoryLimit is reached, so a pod can
    briefly outlive its own. The ReplicaSet is still returned — it genuinely is the
    controller — but resolved=False tells the collector to mark it in payload, so
    the assembler cannot mistake a lookup failure for a pod that has no owner."""
    pod = Obj("pod-1", "api-7f9d4c-xk2mq", [Ref("ReplicaSet", "api-7f9d4c", "rs-gone")])

    owner, resolved = OwnerResolver.build(replicasets=[]).resolve(pod)

    assert owner == Owner(kind="ReplicaSet", name="api-7f9d4c")
    assert resolved is False


def test_standalone_job_resolves_to_itself():
    """A Job run by hand has no CronJob above it. Without mapping intermediates to
    themselves it would fall through to the not-in-map path and be reported as a
    resolution failure, which would be a lie."""
    job = Obj("job-1", "manual-backfill")
    pod = Obj("pod-1", "manual-backfill-abc12", [Ref("Job", "manual-backfill", "job-1")])

    owner, resolved = OwnerResolver.build(jobs=[job]).resolve(pod)

    assert owner == Owner(kind="Job", name="manual-backfill")
    assert resolved is True


def test_cronjob_resolves_through_its_job():
    job = Obj("job-1", "nightly-28123", [Ref("CronJob", "nightly", "cj-1")])
    pod = Obj("pod-1", "nightly-28123-abc12", [Ref("Job", "nightly-28123", "job-1")])

    owner, resolved = OwnerResolver.build(jobs=[job]).resolve(pod)

    assert owner == Owner(kind="CronJob", name="nightly")
    assert resolved is True


# ---- why the map is keyed on uid -------------------------------------------


def test_same_name_different_uid_resolves_separately():
    """Names are reused. Delete a Deployment and recreate it and the new ReplicaSet
    can carry the old name — keying on name would hand the new workload the old
    one's history and poison first_seen_ever(), where a false "seen before" is worst.
    """
    old = Obj("rs-old", "api-7f9d4c", [Ref("Deployment", "api-v1", "dep-old")])
    new = Obj("rs-new", "api-7f9d4c", [Ref("Deployment", "api-v2", "dep-new")])

    resolver = OwnerResolver.build(replicasets=[old, new])

    pod_old = Obj("pod-1", "api-7f9d4c-aaa", [Ref("ReplicaSet", "api-7f9d4c", "rs-old")])
    pod_new = Obj("pod-2", "api-7f9d4c-bbb", [Ref("ReplicaSet", "api-7f9d4c", "rs-new")])

    assert resolver.resolve(pod_old)[0] == Owner(kind="Deployment", name="api-v1")
    assert resolver.resolve(pod_new)[0] == Owner(kind="Deployment", name="api-v2")


def test_replicasets_and_jobs_share_one_map():
    rs = Obj("rs-1", "api-7f9d4c", [Ref("Deployment", "api", "dep-1")])
    job = Obj("job-1", "nightly-28123", [Ref("CronJob", "nightly", "cj-1")])

    resolver = OwnerResolver.build(replicasets=[rs], jobs=[job])

    from_rs = Obj("p1", "api-7f9d4c-aaa", [Ref("ReplicaSet", "api-7f9d4c", "rs-1")])
    from_job = Obj("p2", "nightly-28123-bbb", [Ref("Job", "nightly-28123", "job-1")])

    assert resolver.resolve(from_rs)[0] == Owner(kind="Deployment", name="api")
    assert resolver.resolve(from_job)[0] == Owner(kind="CronJob", name="nightly")
