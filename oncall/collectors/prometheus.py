"""
Firing alerts from Prometheus, polled.

  - GET {ONCALL_PROMETHEUS_URL}/api/v1/alerts: every alert Prometheus is evaluating,
    pending and firing. Polled rather than only pushed, because a webhook that misses a
    delivery misses it for good, and a poll re-reads the whole set every cycle.
  - Scheduled only when ONCALL_PROMETHEUS_URL is set (see config.POLL_INTERVALS).
  - kube-system is not excluded here, unlike the Kubernetes collectors. Those exclude it
    for volume; an alert is already a filtered judgement, and the agent otherwise has no
    way to see a cluster DNS failure (the open question from Phase C). The agent's own
    namespace stays excluded, in collectors/alerts.py.
  - Any failure is unavailable with no signals, like every collector: a partial alert
    list presented as complete would read as alerts that stopped firing.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from typing import Any

from oncall import config
from oncall.collectors import alerts
from oncall.envelope import Signal, SourceStatus

REQUEST_TIMEOUT = 10


def _parse_time(value: str) -> datetime:
    """Prometheus and Alertmanager write nanoseconds and a Z. fromisoformat has read both
    since Python 3.11, truncating past microseconds; the image runs 3.12."""
    return datetime.fromisoformat(value)


def signals_from(body: dict[str, Any], cluster: str,
                 owner_of: alerts.OwnerLookup | None = None) -> list[Signal]:
    """The /api/v1/alerts response into signals. Pure."""
    if body.get("status") != "success":
        raise ValueError(f"Prometheus answered status {body.get('status')!r}")
    out = []
    for a in body.get("data", {}).get("alerts", []):
        if not a.get("activeAt"):
            continue
        s = alerts.to_signal(a.get("labels") or {}, a.get("annotations") or {},
                             _parse_time(a["activeAt"]), a.get("state", "firing"),
                             cluster, owner_of, extra={"via": "prometheus"})
        if s is not None:
            out.append(s)
    return out


def fetch(url: str) -> dict[str, Any]:
    req = urllib.request.Request(f"{url.rstrip('/')}/api/v1/alerts",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:  # noqa: S310 - configured URL
        return json.loads(resp.read())


def collect(cluster: str) -> tuple[SourceStatus, list[Signal], str | None]:
    if not config.PROMETHEUS_URL:
        return SourceStatus.UNAVAILABLE, [], "ONCALL_PROMETHEUS_URL is not set"
    try:
        signals = signals_from(fetch(config.PROMETHEUS_URL), cluster,
                               alerts.buffer_owner_lookup(cluster))
    except Exception as exc:
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"
    return (SourceStatus.OK if signals else SourceStatus.EMPTY), signals, None
