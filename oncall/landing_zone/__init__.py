"""
Public surface of the landing zone. Nothing outside this package issues SQL.

  - This list is the entire vocabulary the rest of the project has for talking to
    storage. A collector or the assembler needing something absent from it is a design
    conversation, not a quick inline SELECT.
  - Callers import from here, so submodules can be split or renamed without rippling.
"""

from oncall.landing_zone.connection import bootstrap, connect
from oncall.landing_zone.reader import (
    first_seen_ever,
    last_run,
    occurrence_times,
    recurrences,
    signals_for_incident,
    signals_in_window,
    source_status_in_window,
)
from oncall.landing_zone.retention import sweep_expired
from oncall.landing_zone.writer import (
    attach_signals,
    close_incident,
    finish_run,
    open_incident,
    record_diagnosis,
    start_run,
    write_signals,
)

__all__ = [
    "attach_signals",
    "bootstrap",
    "close_incident",
    "connect",
    "finish_run",
    "first_seen_ever",
    "last_run",
    "occurrence_times",
    "open_incident",
    "recurrences",
    "record_diagnosis",
    "signals_for_incident",
    "signals_in_window",
    "source_status_in_window",
    "start_run",
    "sweep_expired",
    "write_signals",
]
