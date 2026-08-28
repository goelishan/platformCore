"""
Stage one of the assembler: buffer rows in, one bounded picture of an incident out.

  - Reads through landing_zone.reader and writes nothing. The evidence already exists;
    this layer decides what is worth looking at, never what is true.
  - Collapse happens here because the buffer stores every occurrence separately. Eleven
    rows saying OOMKilled are one fact observed eleven times, and the spacing between
    them is only recoverable while the rows are still apart — which is why the collapse
    could not have happened on the write path.
  - Payload keys are split by whether they moved across occurrences. A key identical in
    all of them describes the problem; a key that changed measures it. The rule is
    structural, so a collector written next year needs no entry in a list here — the
    same argument that made mask-only normalisation beat curated extractors.
  - Sources are reported whether or not they produced anything, and a source that never
    ran at all is reported too. A source that was down and a source that had nothing to
    say are opposite facts, and a bundle that drops the distinction reads as an
    all-clear.
  - Provenance is read and not shown. The collector marks its own pointers and
    bookkeeping, and this layer uses them — trigger_* becomes a stated relationship,
    event_uid guards the counter arithmetic — without ever putting a hash in front of
    a reader. Measured against a live cluster, those keys were four fifths of payload
    bytes and none of them was something anyone could act on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from sqlite3 import Connection
from typing import Any

from pydantic import BaseModel

from oncall import config
from oncall.envelope import (
    Severity,
    SignalKind,
    SignalSource,
    SourceStatus,
    parse,
    provenance_of,
    visible,
)
from oncall.landing_zone import reader


# Severity is a StrEnum, so sorting it sorts alphabetically: 'critical' would land
# above 'error' by luck and 'info' above 'warning' by accident. Ranking has to be
# stated. None ranks below info — an unranked signal is not an urgent one, and the
# collectors that leave severity unset are the ones reporting bookkeeping.
SEVERITY_RANK: dict[str | None, int] = {
    str(Severity.CRITICAL): 4,
    str(Severity.ERROR): 3,
    str(Severity.WARNING): 2,
    str(Severity.INFO): 1,
    None: 0,
}


# Which sources the bundle expects to hear from, taken from the cadence table rather
# than from the collector registry: importing the registry would drag the kubernetes
# client into a layer whose whole contract is that it never touches the cluster.
# registry._validate() already fails at import if the two sets disagree, so this is the
# same set reached by the cheaper route.
EXPECTED_SOURCES: tuple[str, ...] = tuple(str(s) for s in config.POLL_INTERVALS)


# ---- shapes ----------------------------------------------------------------
# Pydantic rather than dataclasses because the receipt this bundle will eventually be
# hashed into needs a canonical serialisation, and model_dump(mode="json") is already
# that. Nothing here computes a hash yet; the shape is chosen so that it can.


class Finding(BaseModel):
    """One distinct problem, and everything the window knows about it."""

    fingerprint: str
    source: SignalSource
    kind: SignalKind
    severity: Severity | None
    namespace: str | None
    subject_name: str | None
    owner_name: str | None
    node_name: str | None

    first_seen: datetime
    last_seen: datetime
    occurrences: int

    # What held still, and what moved. trends carries [first, last] over the
    # occurrences that actually had the key, so a field the collector omits on some
    # polls no longer renders as movement from nothing.
    facts: dict[str, Any]
    trends: dict[str, list[Any]]

    # Keys the collector set on some occurrences and not others. Their value is in
    # facts or trends as usual; this says the series has holes, which is the honest
    # form of a distinction that otherwise reads as a value changing to itself.
    partial: list[str]

    # A representative raw message, kept once rather than per occurrence. The template
    # beside it is what identity is built on, and the two together were 36% of all
    # payload bytes on the live cluster while saying the same sentence twice.
    sample: str | None

    # The fingerprint of the failure that caused this row to be collected, once it has
    # been matched to a Finding in the same bundle. Log excerpts carry a pointer to
    # their trigger; a reader wants the relationship, not the pointer.
    explains: str | None

    # What the collector marked as machinery. Carried so the assembler can use it and
    # kept out of the rendered view. Counter deltas need event_uid, and correlation
    # needs the requested window.
    provenance: dict[str, Any]

    # Provenance, so a Finding can always be taken back to the rows it stands for.
    # Kept because a summary nobody can audit is a summary nobody should trust.
    signal_ids: list[str]


class SourceReport(BaseModel):
    """Whether a source was heard from, and what it managed.

    status is the status of the source's most recent run in the window, not a summary
    of all of them: a source that failed four times and then succeeded reports ok here.
    That is a real limitation and it is stated rather than hidden, because the fix
    (counts per status) is only worth building once the output shows it mattering.
    """

    source: str
    status: SourceStatus
    last_started: datetime | None = None
    signals: int | None = None
    error: str | None = None

    # True when the source produced no collection_runs row in the window at all. Not
    # the same as unavailable: unavailable means it tried and failed, this means
    # nothing tried. Both are absence; only one of them is evidence about the cluster.
    never_ran: bool = False


class Bundle(BaseModel):
    """One incident's evidence, bounded and self-describing."""

    cluster: str
    subject: str | None
    window_start: datetime
    window_end: datetime
    findings: list[Finding]
    sources: list[SourceReport]

    @property
    def signals_read(self) -> int:
        return sum(f.occurrences for f in self.findings)


# ---- payload analysis ------------------------------------------------------


def _payload_of(row: sqlite3.Row) -> dict[str, Any]:
    """A payload that will not parse is not a reason to lose the row.

    The envelope, the timestamps and the fingerprint are all still true, and dropping
    the whole occurrence over a malformed blob would silently change a count that the
    reasoner reads as how often something happened.
    """
    try:
        loaded = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def split_payloads(
    payloads: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[Any]], list[str]]:
    """Constant keys describe the problem; changing keys measure it.

    Keys are visited in sorted order so that two bundles built from the same evidence
    serialise identically — determinism is cheap here and impossible to retrofit once
    something downstream is hashing the result.

    Presence and value are judged separately. A key the collector sets on some polls
    and not others is still constant if every value it did carry was the same, and
    calling that a trend produced the bundle's most confusing line: an endpoint pair
    of a value and itself, which reads as a bug rather than as a gap. The gap is real
    and worth stating, so it is stated as a gap.
    """
    keys = sorted({k for p in payloads for k in p})
    facts: dict[str, Any] = {}
    trends: dict[str, list[Any]] = {}
    partial: list[str] = []

    for key in keys:
        present = [p[key] for p in payloads if key in p]
        if len(present) < len(payloads):
            partial.append(key)

        if all(v == present[0] for v in present):
            facts[key] = present[0]
        else:
            trends[key] = [present[0], present[-1]]

    return facts, trends, partial


# ---- findings --------------------------------------------------------------


MESSAGE_KEY = "message"
TEMPLATE_KEY = "message_template"


def _fold_message(
    facts: dict[str, Any], trends: dict[str, list[Any]]
) -> tuple[dict[str, Any], dict[str, list[Any]], str | None]:
    """Keep the template and one exemplar, never both per occurrence.

    The template is the stable form and is what the fingerprint was built from; the raw
    message is the same sentence with the values filled in. Carrying both for every
    occurrence was 36% of all payload bytes on the live cluster and told a reader
    nothing the template had not already said.

    The exemplar is the most recent raw message, because the concrete numbers in it —
    an actual backoff duration, an actual address — are what a human checks against the
    cluster, and the newest ones are the ones still true. It is dropped entirely when
    no template exists, since then the message is the only statement of the problem and
    is already in facts or trends on its own merits.
    """
    if TEMPLATE_KEY not in facts and TEMPLATE_KEY not in trends:
        return facts, trends, None

    sample: str | None = None
    if MESSAGE_KEY in facts:
        sample = str(facts.pop(MESSAGE_KEY))
    elif MESSAGE_KEY in trends:
        sample = str(trends.pop(MESSAGE_KEY)[-1])

    return facts, trends, sample


def _finding_from(rows: list[sqlite3.Row]) -> Finding:
    """One fingerprint's rows, already ordered by event_time, collapsed to one entry.

    Envelope fields are read off the last row rather than the first. A pod that moved
    node or gained an owner mid-window should be described as it currently stands,
    because the command the reasoner suggests will run against the cluster as it is now.
    """
    raw = [_payload_of(r) for r in rows]
    payloads = [visible(p) for p in raw]

    # Provenance from the most recent occurrence. It is a pointer into other rows, and
    # the newest pointer is the one that still resolves.
    prov = provenance_of(raw[-1])

    facts, trends, partial = split_payloads(payloads)
    facts, trends, sample = _fold_message(facts, trends)
    latest = rows[-1]

    return Finding(
        fingerprint=latest["fingerprint"],
        source=latest["source"],
        kind=latest["kind"],
        severity=latest["severity"],
        namespace=latest["namespace"],
        subject_name=latest["subject_name"],
        owner_name=latest["owner_name"],
        node_name=latest["node_name"],
        first_seen=parse(rows[0]["event_time"]),
        last_seen=parse(latest["event_time"]),
        occurrences=len(rows),
        facts=facts,
        trends=trends,
        partial=[k for k in partial if k in facts or k in trends],
        sample=sample,
        explains=None,
        provenance=prov,
        signal_ids=[r["signal_id"] for r in rows],
    )


def _group_by_fingerprint(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Grouping preserves the order reader.signals_in_window returned, so each group's
    rows stay in event_time order and first/last are simply its ends."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["fingerprint"], []).append(row)
    return list(groups.values())


def _rank(finding: Finding) -> tuple[int, datetime]:
    return (SEVERITY_RANK.get(finding.severity, 0), finding.last_seen)


# ---- correlation -----------------------------------------------------------


TRIGGER_FINGERPRINT = "trigger_fingerprint"


def _link_triggers(findings: list[Finding]) -> None:
    """Turn each log excerpt's pointer into a stated relationship.

    The pointer is set at collection time and is the whole reason a log excerpt can be
    tied to the failure it explains rather than merely sharing a window with it. It is
    resolved here and never rendered: trigger_signal_id takes a new value on every
    poll by construction, so shown verbatim it was the noisiest field in the payload.

    A pointer that resolves to nothing in this bundle is kept rather than cleared. The
    trigger existed — the collector saw it — and it fell outside this window or this
    subject, which is a fact about the bundle's edges and not about the cluster.
    """
    for finding in findings:
        target = finding.provenance.get(TRIGGER_FINGERPRINT)
        if target:
            finding.explains = str(target)


# ---- sources ---------------------------------------------------------------


def _source_reports(conn: Connection, start: datetime, end: datetime) -> list[SourceReport]:
    """Every expected source gets a row, including the ones that said nothing.

    A source missing from collection_runs is the case the three-state status cannot
    express, because three-state describes an attempt and this is the absence of one.
    Left out, it reads to the reasoner as a source that simply had no findings.
    """
    seen = {r["source"]: r for r in reader.source_status_in_window(conn, start, end)}

    reports: list[SourceReport] = []
    for source in EXPECTED_SOURCES:
        row = seen.get(source)
        if row is None:
            reports.append(
                SourceReport(source=source, status=SourceStatus.UNAVAILABLE, never_ran=True)
            )
            continue

        reports.append(
            SourceReport(
                source=source,
                status=row["status"],
                last_started=parse(row["last_started"]),
                signals=row["signal_count"],
                error=row["error"],
            )
        )

    return reports


# ---- assembly --------------------------------------------------------------


def build(
    conn: Connection,
    cluster: str,
    start: datetime,
    end: datetime,
    subject_name: str | None = None,
) -> Bundle:
    """Everything known about one subject over one window, collapsed and ranked.

    The window is passed in rather than derived. Deriving it — back to the last deploy,
    or to first_seen of the worst fingerprint — is a real improvement and a separate
    decision, and making it here would hide the choice inside a function whose output
    is meant to be judged on its own terms.

    No budget and no selection: every finding in the window is returned. That is
    deliberate for a first pass, because the rule for what to leave out cannot be
    written before there is output showing what is worth keeping.
    """
    rows = reader.signals_in_window(
        conn, cluster=cluster, start=start, end=end, subject_name=subject_name
    )

    findings = [_finding_from(group) for group in _group_by_fingerprint(rows)]
    _link_triggers(findings)
    findings.sort(key=_rank, reverse=True)

    return Bundle(
        cluster=cluster,
        subject=subject_name,
        window_start=start,
        window_end=end,
        findings=findings,
        sources=_source_reports(conn, start, end),
    )


# ---- inspection ------------------------------------------------------------
# A human-readable view for judging output by eye. Not the prompt: what the reasoner
# is shown is M5's decision, and binding the two now would mean every wording change
# rewrote the artefact the journal is supposed to identify.


def render(bundle: Bundle, max_value: int = 90) -> str:
    by_fingerprint = {f.fingerprint: f for f in bundle.findings}

    def clip(value: Any) -> str:
        text = str(value).replace("\n", " | ")
        return text if len(text) <= max_value else f"{text[:max_value]}…"

    def label(finding: Finding) -> str:
        named = finding.facts.get("reason") or finding.kind
        return f"{named} on {finding.subject_name}"

    out: list[str] = [
        f"{bundle.subject or 'cluster ' + bundle.cluster}"
        f"  [{bundle.window_start:%H:%M} → {bundle.window_end:%H:%M}]",
        f"{len(bundle.findings)} findings from {bundle.signals_read} signals",
        "",
    ]

    for f in bundle.findings:
        span = f"{f.first_seen:%H:%M} → {f.last_seen:%H:%M}"
        out.append(
            f"[{str(f.severity or '-'):8}] {f.source:11} x{f.occurrences:<3} {span}"
            f"  {f.owner_name or ''}"
        )

        if f.explains:
            trigger = by_fingerprint.get(f.explains)
            out.append(
                f"      explains: {label(trigger)}" if trigger
                else "      explains: a trigger outside this window"
            )

        gap = " (intermittent)"
        for key, value in f.facts.items():
            out.append(f"      {key} = {clip(value)}{gap if key in f.partial else ''}")
        for key, (first, last) in f.trends.items():
            out.append(
                f"      {key} : {clip(first)}  →  {clip(last)}"
                f"{gap if key in f.partial else ''}"
            )
        if f.sample:
            out.append(f"      e.g. {clip(f.sample)}")
        out.append("")

    out.append("sources")
    for s_ in bundle.sources:
        note = "never ran" if s_.never_ran else f"{s_.signals} signals"
        out.append(
            f"      {s_.source:11} {s_.status:12} {note}"
            f"{'  ' + s_.error if s_.error else ''}"
        )

    return "\n".join(out)
