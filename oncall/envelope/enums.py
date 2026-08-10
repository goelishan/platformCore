"""
Closed vocabularies shared by the envelope and the landing zone.

  - StrEnum so a value serialises as its own string with no conversion step.
  - SourceStatus describes a collection attempt, not a signal, and lives on
    collection_runs. Every stored signal is 'ok' by definition: an unavailable
    source produces zero signals.
"""

from __future__ import annotations
from enum import StrEnum

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