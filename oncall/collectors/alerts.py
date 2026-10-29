"""
A Prometheus alert into a Signal, shared by the poller and the Alertmanager webhook.

  - Pure: labels, annotations and times in, a Signal or None out. The poller reads
    /api/v1/alerts and the webhook receives Alertmanager's payload; both arrive here in
    the same shape, so the two paths cannot disagree about what an alert is.
  - The subject is the most specific object the labels name: a pod, then a workload,
    then a node, then the namespace, then the alert itself for cluster-wide rules. A pod
    alert takes its owner from the newest k8s_pods row for that pod, so a crash-looping
    pod's alert lands on the same triage candidate as its pod state, not on a second one.
  - event_time is when the alert went active (activeAt, or startsAt from Alertmanager),
    which is the one stable time an alert has. Re-polling an alert that is still firing
    re-observes the same row; a resolved one is the same row again with severity info.
  - The dedupe key is the alert name plus the labels that identify what fired, never the
    value: a CPU alert at 91% and at 94% is one problem.
  - runbook_url is not carried. The reasoner may not put links in a diagnosis, and a
    link in the evidence is an invitation to; the engineer has the alert itself.
  - Alerts about the agent itself, or from its namespace, are dropped. Diagnosing them
    would make the agent's own trouble a reason to spend on itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from oncall import config
from oncall.envelope import (
    Owner,
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    Subject,
    partition_payload,
)

AGENT_NAMESPACE = "oncall"
AGENT_COMPONENT = "oncall-agent"

# The label that names each kind of subject, most specific first.
WORKLOAD_LABELS = (
    ("deployment", "Deployment"),
    ("statefulset", "StatefulSet"),
    ("daemonset", "DaemonSet"),
    ("job_name", "Job"),
    ("cronjob", "CronJob"),
)

# Labels that identify the object an alert is about. Everything else (instance, job,
# endpoint, service, prometheus, the value) varies by scrape path and would split one
# alert into several fingerprints.
IDENTITY_LABELS = ("namespace", "pod", "container", "node", "persistentvolumeclaim",
                   *(label for label, _ in WORKLOAD_LABELS))

SEVERITIES = {"critical": Severity.CRITICAL, "error": Severity.ERROR,
              "warning": Severity.WARNING, "info": Severity.INFO, "none": Severity.INFO}

# How the alert reached the agent, not what it says: an auditor's field.
PROVENANCE_KEYS = frozenset({"via"})

# A pod's owner, from the buffer: (namespace, pod) -> Owner or None.
OwnerLookup = Callable[[str, str], Owner | None]


def ignored(labels: dict[str, str]) -> bool:
    return (labels.get("alertname") in config.IGNORED_ALERTS
            or labels.get("namespace") == AGENT_NAMESPACE
            or labels.get("component") == AGENT_COMPONENT)


def subject_of(labels: dict[str, str], owner_of: OwnerLookup | None = None
               ) -> tuple[Subject, Owner | None, str | None]:
    """(subject, owner, node) for an alert's labels."""
    ns = labels.get("namespace")
    node = labels.get("node")
    if labels.get("pod") and ns:
        owner = owner_of(ns, labels["pod"]) if owner_of else None
        return Subject(kind="Pod", name=labels["pod"]), owner, node
    for label, kind in WORKLOAD_LABELS:
        if labels.get(label):
            return Subject(kind=kind, name=labels[label]), Owner(kind=kind, name=labels[label]), node
    if node:
        return Subject(kind="Node", name=node), None, node
    if ns:
        return Subject(kind="Namespace", name=ns), None, None
    return Subject(kind="Alert", name=labels.get("alertname", "unnamed")), None, None


def to_signal(
    labels: dict[str, str],
    annotations: dict[str, str],
    active_at: datetime,
    state: str,
    cluster: str,
    owner_of: OwnerLookup | None = None,
    extra: dict[str, Any] | None = None,
) -> Signal | None:
    """state is firing, pending or resolved. None for an alert this agent ignores."""
    if ignored(labels):
        return None

    subject, owner, node = subject_of(labels, owner_of)
    severity = SEVERITIES.get(labels.get("severity", "").lower(), Severity.WARNING)
    if state != "firing":
        # Pending has not met its rule's "for" yet; resolved has stopped. Both are
        # recorded, at info, so the row exists and reads as not currently a problem.
        severity = Severity.INFO

    identity = "|".join(f"{k}={labels[k]}" for k in IDENTITY_LABELS if labels.get(k))
    payload: dict[str, Any] = {
        "reason": labels.get("alertname"),
        "state": state,
        "alert_severity": labels.get("severity"),
        "summary": annotations.get("summary") or annotations.get("message"),
        "description": annotations.get("description"),
        "labels": {k: v for k, v in sorted(labels.items())
                   if k not in ("alertname", "severity", "prometheus")},
        **(extra or {}),
    }
    return Signal(
        source=SignalSource.PROMETHEUS,
        kind=SignalKind.ALERT,
        cluster=cluster,
        event_time=active_at,
        namespace=labels.get("namespace"),
        subject=subject,
        owner=owner,
        node=node,
        severity=severity,
        dedupe_key=f"{labels.get('alertname')}|{identity}",
        payload=partition_payload(payload, PROVENANCE_KEYS),
    )


def buffer_owner_lookup(cluster: str) -> OwnerLookup:
    """Owners from the newest k8s_pods row per pod. Read once per call site, cached."""
    from oncall import landing_zone as lz

    cache: dict[tuple[str, str], Owner | None] = {}

    def lookup(namespace: str, pod: str) -> Owner | None:
        key = (namespace, pod)
        if key not in cache:
            with lz.connect() as conn:
                row = conn.execute(
                    "SELECT owner_kind, owner_name FROM signals WHERE cluster = ? "
                    "AND namespace = ? AND subject_name = ? AND source = 'k8s_pods' "
                    "AND owner_name IS NOT NULL ORDER BY collected_at DESC LIMIT 1",
                    (cluster, namespace, pod),
                ).fetchone()
            cache[key] = Owner(kind=row["owner_kind"], name=row["owner_name"]) if row else None
        return cache[key]

    return lookup
