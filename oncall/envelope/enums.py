"""
Closed vocabularies shared by the envelope and the landing zone.

  - StrEnum so a value serialises as its own string with no conversion step.
  - SourceStatus describes a collection attempt, not a signal, and lives on
    collection_runs. Every stored signal is 'ok' by definition: an unavailable
    source produces zero signals.
  - SignalSource is closed because source names are permanent: they are written
    into every row and are what last_run() and source_status_in_window() key on.
    A free string lets one typo create a phantom source whose runs no staleness
    check will ever look at, and nothing would error.
"""

from __future__ import annotations
from enum import StrEnum


class SignalSource(StrEnum):
    K8S_PODS = "k8s_pods"
    K8S_EVENTS = "k8s_events"
    K8S_LOGS = "k8s_logs"
    PROMETHEUS = "prometheus"
    ARGOCD = "argocd"


class SignalKind(StrEnum):
    POD_STATE = "pod_state"
    EVENT = "event"
    LOG_EXCERPT = "log_excerpt"
    METRIC = "metric"
    DEPLOY = "deploy"

class SourceStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"

class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"