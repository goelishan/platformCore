"""
Canonical time format for anything that crosses a boundary.

  - Fixed width, always UTC, always six fractional digits, always 'Z'.
  - Fixed width is what makes lexicographic ordering equal chronological ordering,
    which is what lets event_time live in a TEXT column and still range-scan.
  - isoformat() alone omits microseconds when they are zero, so the same instant
    would hash two different ways and land in two rows.
  - Both the identity hash and the row writer call iso(). If those ever formatted
    time differently, idempotency would break silently.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected; nothing tz-naive crosses a boundary")
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse(text: str) -> datetime:
    """Z is normalised explicitly rather than leaning on fromisoformat's own support
    for it, which only arrived in 3.11 and is easy to lose on an older runtime."""
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text).astimezone(UTC)
