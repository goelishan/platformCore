"""
Argo CD Application state: what is deployed from git, and whether it synced and is healthy.

  - Two uses. A Degraded or OutOfSync Application, or a failed sync, is often the cause
    of what the pods show (a bad commit, a sync that half applied). And every Application
    tells the reasoner which namespace is managed from git, so a remedy changes the
    source rather than the live object Argo CD will revert within minutes.
  - One signal per Application, written into its destination namespace: the namespace
    whose workloads it manages, which is how the assembler joins it to them. The object's
    own namespace (argocd) is kept in the payload.
  - Healthy and Synced Applications are recorded too, at info. "Managed by Argo CD" is
    evidence the remedy rule needs whether or not anything is wrong with the sync.
  - event_time is the last deploy (status.history) or the last sync operation, else the
    Application's creation. Not reconciledAt, which moves every few minutes and would
    mint a new row per poll for an Application that has not changed. Like k8s_services,
    a derived time: current state is the newest collected_at, not the event time.
  - No Application CRD is not an error: Argo CD is simply not installed (kind), and the
    run is empty. Anything else that fails is unavailable with no signals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from oncall import config
from oncall.collectors import k8s_client as k8s
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    partition_payload,
)

GROUP, VERSION, PLURAL = "argoproj.io", "v1alpha1", "applications"
PAGE_SIZE = 200
MAX_RESOURCES = 10

PROVENANCE_KEYS = frozenset({"app_uid"})

# Health first, because an unhealthy Application is a fault in the cluster now; sync
# state is a fault in how it got there.
HEALTH_SEVERITY = {"Degraded": Severity.ERROR, "Missing": Severity.ERROR,
                   "Unknown": Severity.WARNING, "Progressing": Severity.INFO,
                   "Suspended": Severity.INFO, "Healthy": Severity.INFO}
FAILED_PHASES = {"Failed", "Error"}


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def event_time(app: dict[str, Any]) -> datetime:
    status = app.get("status") or {}
    history = status.get("history") or []
    for candidate in (
        history[-1].get("deployedAt") if history else None,
        (status.get("operationState") or {}).get("finishedAt"),
        app.get("metadata", {}).get("creationTimestamp"),
    ):
        when = _time(candidate)
        if when:
            return when
    return datetime.fromtimestamp(0, UTC)


def assess(app: dict[str, Any]) -> tuple[str, Severity]:
    """(reason, severity). The worst of health, sync and the last operation."""
    status = app.get("status") or {}
    health = (status.get("health") or {}).get("status") or "Unknown"
    sync = (status.get("sync") or {}).get("status") or "Unknown"
    phase = (status.get("operationState") or {}).get("phase")

    if phase in FAILED_PHASES:
        return "SyncFailed", Severity.ERROR
    severity = HEALTH_SEVERITY.get(health, Severity.WARNING)
    if severity in (Severity.ERROR, Severity.WARNING):
        return health, severity
    if sync == "OutOfSync":
        return "OutOfSync", Severity.WARNING
    return ("Healthy" if health == "Healthy" else health), Severity.INFO


def signal_for(app: dict[str, Any], cluster: str) -> Signal | None:
    meta = app.get("metadata") or {}
    spec = app.get("spec") or {}
    status = app.get("status") or {}
    dest = (spec.get("destination") or {}).get("namespace")
    if not k8s.in_scope(dest):
        return None

    reason, severity = assess(app)
    source = spec.get("source") or (spec.get("sources") or [{}])[0]
    op = status.get("operationState") or {}
    unhealthy = [
        {"kind": r.get("kind"), "name": r.get("name"), "namespace": r.get("namespace"),
         "sync": r.get("status"), "health": (r.get("health") or {}).get("status"),
         "message": (r.get("health") or {}).get("message")}
        for r in status.get("resources") or []
        if r.get("status") != "Synced" or (r.get("health") or {}).get("status")
        not in (None, "Healthy")
    ]
    payload: dict[str, Any] = {
        "reason": reason,
        "managed_by": "argocd",
        "app_namespace": meta.get("namespace"),
        "health": (status.get("health") or {}).get("status"),
        "sync": (status.get("sync") or {}).get("status"),
        "revision": (status.get("sync") or {}).get("revision"),
        "repo": source.get("repoURL"),
        "path": source.get("path") or source.get("chart"),
        "target_revision": source.get("targetRevision"),
        "auto_sync": bool((spec.get("syncPolicy") or {}).get("automated")),
        "self_heal": bool(((spec.get("syncPolicy") or {}).get("automated") or {}).get("selfHeal")),
        "operation": op.get("phase"),
        "operation_message": op.get("message"),
        "out_of_sync_or_unhealthy": unhealthy[:MAX_RESOURCES] or None,
        "more_resources": max(0, len(unhealthy) - MAX_RESOURCES) or None,
        "app_uid": meta.get("uid"),
    }
    return Signal(
        source=SignalSource.ARGOCD,
        kind=SignalKind.DEPLOY,
        cluster=cluster,
        event_time=event_time(app),
        namespace=dest,
        subject=Subject(kind="Application", name=meta.get("name", "unnamed"), uid=meta.get("uid")),
        owner=None,
        node=None,
        severity=severity,
        dedupe_key=reason,
        payload=partition_payload(payload, PROVENANCE_KEYS),
    )


def list_applications() -> list[dict[str, Any]]:
    api = k8s.custom_objects()
    items: list[dict[str, Any]] = []
    cont = None
    for _ in range(k8s.MAX_PAGES):
        page = api.list_namespaced_custom_object(
            GROUP, VERSION, config.ARGOCD_NAMESPACE, PLURAL, limit=PAGE_SIZE,
            _continue=cont, _request_timeout=k8s.REQUEST_TIMEOUT,
        )
        items.extend(page.get("items") or [])
        cont = (page.get("metadata") or {}).get("continue")
        if not cont:
            return items
    raise RuntimeError("Application list did not terminate; refusing a partial result")


def collect(cluster: str) -> tuple[SourceStatus, list[Signal], str | None]:
    try:
        apps = list_applications()
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return SourceStatus.EMPTY, [], None
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"
    signals = [s for s in (signal_for(a, cluster) for a in apps) if s is not None]
    return (SourceStatus.OK if signals else SourceStatus.EMPTY), signals, None
