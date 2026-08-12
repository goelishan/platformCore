"""
Public surface of the local buffer. Nothing outside this package issues SQL against it.

  - This list is the entire vocabulary the rest of the project has for talking to the
    buffer. A collector or the assembler needing something absent from it is a design
    conversation, not a quick inline SELECT.
  - Callers import from here, so submodules can be split or renamed without rippling.
  - The buffer answers "what is happening now". History lives in oncall.store, and
    first_seen_ever moved there for that reason: asked against a two-day buffer it
    would report "never seen before" for anything older, which is the most confident
    possible way to be wrong.
"""

from oncall.landing_zone.connection import bootstrap, connect
from oncall.landing_zone.outbox import (
    clear_through,
    depth,
    enqueue,
    pending,
    unshipped_signal_ids,
)
from oncall.landing_zone.reader import (
    buffer_drops_in_window,
    last_run,
    last_shipping_run,
    occurrence_times,
    recurrences,
    signals_for_incident,
    signals_in_window,
    source_status_in_window,
)
from oncall.landing_zone.retention import reclaim, sweep_blobs, sweep_buffer
from oncall.landing_zone.writer import (
    attach_signals,
    close_incident,
    finish_run,
    finish_shipping_run,
    open_incident,
    record_buffer_drop,
    record_diagnosis,
    start_run,
    start_shipping_run,
    write_signals,
)

__all__ = [
    "attach_signals",
    "bootstrap",
    "buffer_drops_in_window",
    "clear_through",
    "close_incident",
    "connect",
    "depth",
    "enqueue",
    "finish_run",
    "finish_shipping_run",
    "last_run",
    "last_shipping_run",
    "occurrence_times",
    "open_incident",
    "pending",
    "reclaim",
    "record_buffer_drop",
    "record_diagnosis",
    "recurrences",
    "signals_for_incident",
    "signals_in_window",
    "source_status_in_window",
    "start_run",
    "start_shipping_run",
    "sweep_blobs",
    "sweep_buffer",
    "unshipped_signal_ids",
    "write_signals",
]
