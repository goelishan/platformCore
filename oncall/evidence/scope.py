"""
Incident scope: which buffer rows belong to one subject, and on what grounds.

  - The envelope was designed so correlation is one join. Filtering on subject_name
    alone is not that join: a pod that cannot schedule because both workers are
    NotReady carries none of the node rows that explain it, and a bundle built from
    its own rows presents the symptom as the whole story.
  - Every admitted row is labelled with the rule that admitted it. The label is a
    fact about the bundle, not about the cluster: a Service admitted for sharing a
    namespace is a weaker claim than a row admitted by subject, and the reasoner has
    to be able to weigh the two differently.
  - Structural rules only. The scheduler's message names the taint that blocked the
    pod, and matching that text against node taints would be a curated extractor
    under another name. An unscheduled pod was weighed against every node, so every
    node is in scope; that is a fact about scheduling, not a reading of a sentence.
  - Pure over rows. build() hands in the window and this module decides membership,
    so the rules are testable without a buffer and cannot issue a query of their own.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from pydantic import BaseModel

from oncall.envelope import SignalSource

JOIN_SUBJECT = "subject"
JOIN_OWNER = "owner"
JOIN_NODE = "node"
JOIN_UNSCHEDULED = "unscheduled"
JOIN_NAMESPACE = "namespace"
JOIN_CLUSTER = "cluster"

# Strongest first. A row several rules admit carries the strongest label, because the
# label says how directly the row bears on the subject.
JOIN_ORDER = (
    JOIN_SUBJECT,
    JOIN_OWNER,
    JOIN_NODE,
    JOIN_UNSCHEDULED,
    JOIN_NAMESPACE,
    JOIN_CLUSTER,
)

NODE_KIND = "Node"


class Scope(BaseModel):
    """What one subject resolved to inside one window."""

    subject: str
    found: bool = False
    namespace: str | None = None
    owner: str | None = None
    nodes: list[str] = []

    # A pod of this workload was seen on no node. The scheduler weighed it against
    # every node, so every node's state is admissible evidence for why it is Pending.
    unscheduled: bool = False


def resolve(rows: Iterable[sqlite3.Row], subject: str) -> Scope:
    """Read the subject's namespace, owner and nodes off its own rows.

    The newest row wins for namespace and owner, as in _finding_from: whatever the
    reasoner suggests runs against the cluster as it stands now. Nodes are every node
    the workload was seen on, not only the latest: a pod that moved mid-window may
    have moved because of the first one.

    A subject that is itself an owner, a Deployment name, resolves through owner_name.
    Naming the workload rather than one pod is the stable way to ask, since the pod's
    name does not survive its replacement.
    """
    rows = list(rows)
    own = [r for r in rows if r["subject_name"] == subject]
    owned = [r for r in rows if r["owner_name"] == subject]
    if not own and not owned:
        return Scope(subject=subject)

    anchor = (own or owned)[-1]
    if anchor["subject_kind"] == NODE_KIND:
        return Scope(subject=subject, found=True, nodes=[subject])

    # Either branch lands here with the right owner: an owned row's owner_name is the
    # subject by definition, so no special case is needed for a workload name.
    namespace = anchor["namespace"]
    owner = anchor["owner_name"]
    members = [
        r
        for r in rows
        if r["namespace"] == namespace
        and (r["subject_name"] == subject or (owner and r["owner_name"] == owner))
    ]

    # Only pod state says where a pod runs. Events and log excerpts leave node_name
    # empty by construction, so reading their absence as "unscheduled" would pull
    # every node into the scope of a workload that is placed and simply noisy.
    pods = [r for r in members if r["source"] == SignalSource.K8S_PODS]
    return Scope(
        subject=subject,
        found=True,
        namespace=namespace,
        owner=owner,
        nodes=sorted({r["node_name"] for r in members if r["node_name"]}),
        unscheduled=any(not r["node_name"] for r in pods),
    )


def admits(row: sqlite3.Row, scope: Scope) -> str | None:
    """The strongest rule under which this row belongs to the scope, or None."""
    if not scope.found:
        return None

    name, kind, namespace = row["subject_name"], row["subject_kind"], row["namespace"]
    if name == scope.subject and namespace == scope.namespace:
        return JOIN_SUBJECT
    if scope.owner and row["owner_name"] == scope.owner and namespace == scope.namespace:
        return JOIN_OWNER
    if kind == NODE_KIND and name in scope.nodes:
        return JOIN_NODE
    if kind == NODE_KIND and scope.unscheduled:
        return JOIN_UNSCHEDULED

    # Co-location, not selection. Pod rows carry no labels, so a Service's selector
    # cannot be matched against them; sharing a namespace is the most this can claim,
    # and the label says so rather than dressing it as a match.
    if (
        row["source"] == SignalSource.K8S_SERVICES
        and scope.namespace is not None
        and namespace == scope.namespace
    ):
        return JOIN_NAMESPACE
    return None


def select(
    rows: Iterable[sqlite3.Row], scope: Scope
) -> tuple[list[sqlite3.Row], dict[str, str]]:
    """Rows in scope, in their original order, and the rule that admitted each."""
    kept: list[sqlite3.Row] = []
    joins: dict[str, str] = {}
    for row in rows:
        join = admits(row, scope)
        if join is not None:
            kept.append(row)
            joins[row["signal_id"]] = join
    return kept, joins


def strongest(joins: Iterable[str]) -> str:
    return min(joins, key=JOIN_ORDER.index)