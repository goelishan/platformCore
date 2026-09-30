"""
The assembler: buffer rows in, one bounded picture of an incident out.

  - Reads through landing_zone.reader and store, and writes nothing. The evidence
    already exists; this layer decides what is worth looking at, never what is true.
  - Collapse happens here because the buffer stores every occurrence separately. Eleven
    rows saying OOMKilled are one fact observed eleven times, and the spacing between
    them is only recoverable while the rows are still apart.
  - Provenance is read and not shown. The collector marks its own pointers and
    bookkeeping; this layer uses them — trigger_* becomes a stated relationship,
    event_uid guards the counter arithmetic — without putting a hash in front of a
    reader.
  - Everything the bundle cannot say, it says it cannot say. Findings left out for
    budget are recorded, claims dropped for want of history are recorded, a source that
    never ran is recorded, and a delta that cannot be computed carries the reason
    instead of a number. A bundle that omits silently reads as complete, which is the
    one failure mode a reasoner cannot detect from the inside.
  - Two receipts, because one hash cannot carry two promises. bundle_sha256 says which
    evidence was selected and survives a reworded template; prompt_sha256 says which
    bytes reached the model and catches a template that dropped a section.
  - Nothing here writes. An incident is opened by whoever declared it — a UI, an alert
    webhook — and attach_signals is that caller's to make, from candidate_signal_ids.
    The assembler is invoked repeatedly while a human iterates, and a reader that
    mutated the incident on every pass would make the record a function of how many
    times someone hit refresh.

    Those ids are every signal in the window, not the findings that survived the
    budget. Incident membership is a fact about the cluster's timeline; the budget is a
    fact about a prompt. Conflating them would let a prompt-budget change quietly
    rewrite what the incident consisted of.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import Any

from pydantic import BaseModel

from oncall import config
from oncall.envelope import (
    Severity,
    SignalKind,
    SignalSource,
    SourceStatus,
    iso,
    parse,
    provenance_of,
    visible,
)
from oncall.evidence.scope import JOIN_CLUSTER, Scope, resolve, select, strongest
from oncall.landing_zone import blobs, reader


# Severity is a StrEnum, so sorting it sorts alphabetically: 'critical' would land
# above 'error' by luck and 'info' above 'warning' by accident. Ranking has to be
# stated. None ranks below info — an unranked signal is not an urgent one.
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
# registry._validate() already fails at import if the two sets disagree.
EXPECTED_SOURCES: tuple[str, ...] = tuple(str(s) for s in config.POLL_INTERVALS)


# A collection attempt that saw nothing, in the closed vocabulary the envelope owns.
# Matched by value rather than by key name so a collector written later inherits the
# behaviour by using the same three states, which is the convention already, instead
# of by being added to a list in this file.
BLIND_STATUSES = frozenset({str(SourceStatus.EMPTY), str(SourceStatus.UNAVAILABLE)})

MESSAGE_KEY = "message"
TEMPLATE_KEY = "message_template"
EVENT_UID = "event_uid"
FIRST_TIMESTAMP = "api_first_timestamp"
TRIGGER_FINGERPRINT = "trigger_fingerprint"

# Older than any row this system can hold, so a lower bound that excludes nothing.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Where a finding's severity came from. Every source but one judges severity on the
# four-level scale. k8s_events copies Kubernetes' own Normal/Warning, and stamps
# severity_basis="cluster" into its provenance to say so. Absent means assessed.
BASIS_ASSESSED = "assessed"
BASIS_CLUSTER = "cluster"

WINDOW_FIXED = "fixed lookback"
WINDOW_EXTENDED = "extended to the first occurrence of the leading finding"
WINDOW_CAPPED = "extended to the lookback cap, which the leading finding predates"


# ---- shapes ----------------------------------------------------------------
# Pydantic rather than dataclasses because the receipt needs a canonical
# serialisation, and model_dump(mode="json") already is one.


class Spread(BaseModel):
    """How far the same problem reaches, beyond whatever the caller was looking at.

    Five pods failing across five nodes and five pods failing on one node are different
    diagnoses and lead to different first commands. A bundle scoped to one pod cannot
    see the difference from its own evidence, so it has to ask.
    """

    subjects: int
    nodes: int
    namespaces: int
    clusters: int


class Spacing(BaseModel):
    """The rhythm of a recurring problem, where the observations can resolve one.

    Occurrences are never collapsed on write precisely so this survives: first, last and
    a count cannot distinguish exponential backoff from something external arriving on a
    fixed cycle, and the gaps can.

    shape is 'unresolved' whenever the collector's cadence is coarse enough to be what
    is being measured. Poll every sixty seconds and every gap is a multiple of sixty
    whatever the workload does, so a confident 'regular' would be describing this agent.
    """

    gaps_seconds: list[int]
    shape: str
    note: str


class Promotion(BaseModel):
    """An excerpt replaced by the bytes the container actually emitted.

    The excerpt is lossy by construction — trimmed, collapsed, byte-capped — and the
    blob is not. Promotion buys the loss back where it costs most, under a budget,
    because the whole point of the excerpt was that the unabridged form does not fit.

    status carries read_blob's four outcomes rather than a bool. missing is retention
    doing its job; gone and corrupt are the write path or the sweep being broken, and
    collapsing them into "no text" would hide two bugs behind one routine event.
    """

    fingerprint: str
    blob_id: str
    status: str
    bytes: int = 0
    text: str | None = None
    reason: str = ""


class Loss(BaseModel):
    """What the buffer gave up over a window that overlaps this one.

    Consulted before anything is concluded from a gap. Without it a dropped window and
    a quiet window are the same shape, which is the failure three-state source status
    exists to prevent, one tier down.
    """

    reason: str
    rows_dropped: int

    # Of those, how many never reached the store. Shipped rows are still answerable
    # from history; these are gone, and a gap covering them is not a quiet stretch.
    unshipped: int = 0

    window_start: str | None
    window_end: str | None


class Delta(BaseModel):
    """What a counter did over the window, and how confidently that can be said.

    change is optional on purpose. A counter is only meaningful relative to the
    lifetime of the thing counting it, and where that lifetime cannot be established
    the honest answer is the absolute value plus a reason, never a subtraction across
    a boundary that may not exist.
    """

    key: str
    first: int
    last: int
    change: int | None
    basis: str
    series: int = 1


class History(BaseModel):
    """What the store knows that a two-day buffer cannot."""

    first_seen_ever: datetime | None = None

    # None when the store began watching this cluster after the window started. Its
    # "no earlier row" would then describe where its memory begins, not the problem.
    is_new: bool | None = None

    # The store's horizon for this cluster, so a reader can see what "first seen" and
    # "new" were measured against.
    watching_since: datetime | None = None

    # How many clusters have ever reported this fingerprint. A store question, not a
    # buffer one: the buffer holds one cluster's recent rows and would answer "one"
    # with total confidence.
    clusters: int | None = None


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

    facts: dict[str, Any]
    trends: dict[str, list[Any]]
    partial: list[str]
    sample: str | None
    deltas: list[Delta] = []
    explains: str | None = None
    history: History | None = None
    spread: Spread | None = None
    spacing: Spacing | None = None

    # Blob ids seen across the occurrences, newest last. Carried so promotion has
    # something to promote; the excerpt in facts is the trimmed form of the newest.
    blob_ids: list[str] = []

    # The scope rule that admitted this finding: subject, owner, node, unscheduled,
    # namespace, or cluster when no subject was named. How directly the finding bears
    # on the subject, stated rather than left for the reader to infer from names.
    joined_by: str | None = None

    # Whether severity is this agent's judgement (assessed) or a copy of Kubernetes'
    # Normal/Warning label (cluster). Same column, different meaning: a Normal
    # NodeNotReady event reads info beside a k8s_nodes row calling the same node
    # critical, and both are right. Carried so nothing downstream reads them as one scale.
    severity_basis: str = BASIS_ASSESSED

    # True when the finding's own status says it saw nothing. Such a row reports on
    # this agent's visibility rather than on the cluster, and severity was inherited
    # from whatever triggered the read — so left alone it can outrank the failure it
    # was supposed to explain while containing no evidence at all.
    #
    # Only ever as good as the status field it reads. A read that returned an
    # infrastructure error under a 200 still claims ok, and will still rank as though
    # it carried evidence, which is why that detection belongs on the write path.
    observational: bool = False

    # What the collector marked as machinery. Carried so the assembler can use it and
    # kept out of the rendered view.
    provenance: dict[str, Any]
    signal_ids: list[str]

    @property
    def rank(self) -> int:
        """Severity as stated, capped by what the finding actually says.

        The cap rather than a demotion to the bottom: not being able to see is a real
        finding and sometimes the most important one, it is just never more urgent
        than the evidence it failed to provide. Unavailable caps higher than empty
        because a failed read and a container that printed nothing are different
        facts, and only one of them is about this agent being blind.
        """
        base = SEVERITY_RANK.get(self.severity, 0)
        if not self.observational:
            return base

        cap = SEVERITY_RANK[str(Severity.WARNING)] if self.blind else SEVERITY_RANK[
            str(Severity.INFO)
        ]
        return min(base, cap)

    @property
    def blind(self) -> bool:
        return str(SourceStatus.UNAVAILABLE) in _status_values(self)


class Omission(BaseModel):
    """A finding the bundle chose not to carry.

    Recorded rather than dropped, exactly like declined targets, buffer drops and a
    truncated excerpt. The selection is the assembler's version of the target cap, and
    the same rule applies to it: what was left out is part of what the diagnosis was
    made against.
    """

    fingerprint: str
    source: str
    severity: str | None
    occurrences: int
    reason: str


class SourceReport(BaseModel):
    """Whether a source was heard from, and what it managed.

    status is the status of the source's most recent run in the window, not a summary
    of all of them: a source that failed four times and then succeeded reports ok here.
    Stated rather than hidden, because the fix is only worth building once the output
    shows it mattering.
    """

    source: str
    status: SourceStatus
    last_started: datetime | None = None
    signals: int | None = None
    error: str | None = None

    # Every attempt in the window and how many of them failed, which status alone
    # cannot say: a source that failed four times and then succeeded reports ok, and a
    # bundle drawing conclusions from its quiet stretch needs to know about the four.
    attempts: int = 0
    failed: int = 0

    # No collection_runs row in the window at all. Not the same as unavailable:
    # unavailable means it tried and failed, this means nothing tried. Both are
    # absence; only one of them is evidence about the cluster.
    never_ran: bool = False


class Bundle(BaseModel):
    """One incident's evidence, bounded and self-describing."""

    cluster: str
    subject: str | None
    window_start: datetime
    window_end: datetime
    window_basis: str = WINDOW_FIXED

    # What the subject resolved to. None for a cluster-wide bundle, and found=False
    # when the subject produced nothing in the window, which is not the same as a
    # subject that is healthy.
    scope: Scope | None = None

    findings: list[Finding]
    sources: list[SourceReport]
    omitted: list[Omission] = []
    promoted: list[Promotion] = []
    losses: list[Loss] = []

    # Every signal in the window, whether or not its finding survived the budget. What
    # the caller passes to attach_signals if it decides this bundle belongs to an
    # incident; see the note at the top about why the assembler does not do that itself.
    candidate_signal_ids: list[str] = []

    # Claims this bundle would normally make and could not. Read by the reasoner as
    # prose and by the eval harness as a field, which is why it is a list of strings
    # on the object rather than a sentence inside a rendered prompt.
    degraded: list[str] = []

    # Which keying rules produced the fingerprints in here. More than one means a
    # recurrence query spanned a normalizer change, so "first seen" and "new" are
    # answers about the rules as much as about the cluster.
    normalizer_versions: list[str] = []

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


def _status_values(finding: Finding) -> set[str]:
    values = {str(v) for v in finding.facts.values()}
    for pair in finding.trends.values():
        values.update(str(v) for v in pair)
    return values


def split_payloads(
    payloads: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[Any]], list[str]]:
    """Constant keys describe the problem; changing keys measure it.

    Keys are visited in sorted order so that two bundles built from the same evidence
    serialise identically — determinism is cheap here and impossible to retrofit once
    something downstream is hashing the result.

    Presence and value are judged separately. A key the collector sets on some polls
    and not others is still constant if every value it did carry was the same, and
    calling that a trend produced an endpoint pair of a value and itself, which reads
    as a bug rather than as a gap. The gap is real and is reported as a gap.
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


def _fold_message(
    facts: dict[str, Any], trends: dict[str, list[Any]]
) -> tuple[dict[str, Any], dict[str, list[Any]], str | None]:
    """Keep the template and one exemplar, never both per occurrence.

    The template is the stable form and is what the fingerprint was built from; the raw
    message is the same sentence with the values filled in. Carrying both for every
    occurrence was 36% of all payload bytes on the live cluster and told a reader
    nothing the template had not already said.

    The exemplar is the most recent raw message, because the concrete numbers in it are
    what a human checks against the cluster and the newest ones are the ones still
    true. Dropped entirely when there is no template, since then the message is the
    only statement of the problem.
    """
    if TEMPLATE_KEY not in facts and TEMPLATE_KEY not in trends:
        return facts, trends, None

    sample: str | None = None
    if MESSAGE_KEY in facts:
        sample = str(facts.pop(MESSAGE_KEY))
    elif MESSAGE_KEY in trends:
        sample = str(trends.pop(MESSAGE_KEY)[-1])

    return facts, trends, sample


# ---- counters --------------------------------------------------------------
# Read-time work M2 deliberately deferred. Absolute counts are stored because a
# collector cannot see the previous poll; the subtraction belongs wherever a series
# of observations exists, which is here.


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _series_of(observations: list[tuple[str | None, int]]) -> dict[str | None, list[int]]:
    grouped: dict[str | None, list[int]] = {}
    for uid, value in observations:
        grouped.setdefault(uid, []).append(value)
    return grouped


def _counts(observations: list[tuple[str | None, int]]) -> bool:
    """Whether these observations behave like a counter within each series.

    Non-decreasing inside a series is the whole definition. Across series it may fall
    freely — that is a reset, and handling it is the point — but a value that drops
    while the thing reporting it stayed the same is measuring a state, not accumulating
    a count.

    Deliberately not a list of counter field names. A new collector inherits this by
    reporting numbers that behave like counters, rather than by being added here.
    """
    for values in _series_of(observations).values():
        if any(b < a for a, b in zip(values, values[1:], strict=False)):
            return False
    return True


def _delta_for(
    key: str, observations: list[tuple[str | None, int]], series_started_inside: bool
) -> Delta:
    """One counter's change, grouped by whatever was doing the counting.

    Aggregation is client-side and cached in the reporting component's memory, so a
    kubelet restart abandons one object and starts another from one. Diffing straight
    across that boundary invents a decrease that never happened, which is the same
    reset handling rate() needs when the process exporting a metric restarts.

    A series that appears after the first observation began inside the window, so its
    whole value is change. A series present from the first observation contributes only
    the difference across it — anything it counted before the window is not this
    window's evidence.

    With a single observation and a series older than the window there is nothing to
    subtract from, and the absolute value is reported with change left unset. Where
    the series is known to have started inside the window the absolute value *is* the
    change, and api_first_timestamp is what establishes that.
    """
    grouped = _series_of(observations)
    first, last = observations[0][1], observations[-1][1]

    if len(observations) == 1:
        if series_started_inside:
            return Delta(
                key=key, first=first, last=last, change=last,
                basis="one observation of a series that began inside the window",
            )
        return Delta(
            key=key, first=first, last=last, change=None,
            basis="one observation of a series older than the window; value is absolute",
        )

    leading = observations[0][0]
    total = 0
    for uid, values in grouped.items():
        total += values[-1] if uid != leading else values[-1] - values[0]

    if len(grouped) > 1:
        basis = f"summed across {len(grouped)} counting series, later ones taken whole"
    else:
        basis = f"diffed across {len(observations)} observations of one series"

    return Delta(key=key, first=first, last=last, change=total, basis=basis, series=len(grouped))


def _counting_series(payload: dict[str, Any], prov: dict[str, Any]) -> str | None:
    """Which object was doing the counting, wherever the row happens to carry it.

    event_uid moved into _provenance, and a window spanning that change holds rows of
    both shapes. Reading only the new location gives the older rows a series identity
    of None, so one uninterrupted counter looks like two and its total is summed as
    though the second had started from zero — a phantom reset produced entirely by the
    migration, in the field whose whole purpose is to make real resets visible.
    """
    return prov.get(EVENT_UID) or payload.get(EVENT_UID)


def _deltas(
    rows: list[sqlite3.Row],
    payloads: list[dict[str, Any]],
    provenances: list[dict[str, Any]],
    trends: dict[str, list[Any]],
    facts: dict[str, Any],
    window_start: datetime,
) -> list[Delta]:
    """Every integer-valued trend gets a change, and single-occurrence findings get one
    where the series can be shown to have started inside the window.

    Integer-valued rather than a list of counter names: this reports the change in a
    number over a window, which is true of any number, and what the change *means* is
    the reasoner's to decide. A curated list of counters would be one more thing a new
    collector has to be added to.
    """
    started_inside = _series_started_inside(facts, window_start)

    keys = sorted(set(trends) | ({k for k in facts} if len(rows) == 1 else set()))
    deltas: list[Delta] = []

    for key in keys:
        observations: list[tuple[str | None, int]] = []
        for payload, prov in zip(payloads, provenances, strict=True):
            value = _int(payload.get(key))
            if value is not None:
                observations.append((_counting_series(payload, prov), value))

        if not observations:
            continue
        if len(observations) == 1 and not started_inside:
            # Nothing to say that facts does not already say, unless the series can be
            # placed inside the window. Emitting a Delta with no change for every
            # integer on a one-row finding is noise wearing the shape of analysis.
            continue
        if not _counts(observations):
            # An integer that goes down inside one series is not counting anything, so
            # a difference across it is not an amount of something that happened.
            # container_lifetime_seconds is the live example: 59 then 51 is two
            # containers that died after different intervals, and "-8" describes
            # nothing that occurred. It stays a trend, which is what it is.
            continue

        deltas.append(_delta_for(key, observations, started_inside))

    return deltas


def _series_started_inside(facts: dict[str, Any], window_start: datetime) -> bool:
    """Whether the counting began inside the window, per the API's own first timestamp.

    This is the field that turns an uninterpretable absolute into a rate. Dropped from
    the bundle as provenance it looked like noise; it is the denominator.
    """
    stamp = facts.get(FIRST_TIMESTAMP)
    if not isinstance(stamp, str):
        return False
    try:
        return parse(stamp) >= window_start
    except ValueError:
        return False


# ---- findings --------------------------------------------------------------


def _finding_from(rows: list[sqlite3.Row], window_start: datetime) -> Finding:
    """One fingerprint's rows, already ordered by event_time, collapsed to one entry.

    Envelope fields are read off the last row rather than the first. A pod that moved
    node or gained an owner mid-window should be described as it currently stands,
    because the command the reasoner suggests will run against the cluster as it is now.
    """
    raw = [_payload_of(r) for r in rows]
    payloads = [visible(p) for p in raw]
    provenances = [provenance_of(p) for p in raw]

    facts, trends, partial = split_payloads(payloads)
    facts, trends, sample = _fold_message(facts, trends)
    latest = rows[-1]

    finding = Finding(
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
        deltas=_deltas(rows, payloads, provenances, trends, facts, window_start),
        # Provenance from the most recent occurrence: it is a pointer into other rows,
        # and the newest pointer is the one that still resolves.
        provenance=provenances[-1],
        signal_ids=[r["signal_id"] for r in rows],
        # Content-addressed, so an unchanged log across polls is one id seen twice.
        # dict.fromkeys keeps first-seen order while dropping the repeat.
        blob_ids=list(dict.fromkeys(r["blob_id"] for r in rows if r["blob_id"])),
    )
    finding.observational = bool(_status_values(finding) & BLIND_STATUSES)
    finding.severity_basis = str(finding.provenance.get("severity_basis", BASIS_ASSESSED))
    return finding


def _group_by_fingerprint(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Grouping preserves the order reader.signals_in_window returned, so each group's
    rows stay in event_time order and first/last are simply its ends."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["fingerprint"], []).append(row)
    return list(groups.values())


def _order(finding: Finding) -> tuple[int, bool, datetime]:
    """Rank first. At equal rank, a judgement leads a transcribed label.

    A tie-break and not a demotion: a Kubernetes Warning still outranks an assessed
    info. What it settles is two findings at the same level, where the one this agent
    assessed says more than the one it copied.
    """
    return (finding.rank, finding.severity_basis == BASIS_ASSESSED, finding.last_seen)


# ---- correlation -----------------------------------------------------------


def _link_triggers(findings: list[Finding]) -> None:
    """Turn each log excerpt's pointer into a stated relationship.

    The pointer is set at collection time and is the whole reason an excerpt can be
    tied to the failure it explains rather than merely sharing a window with it. It is
    resolved here and never rendered: trigger_signal_id takes a new value on every poll
    by construction.

    A pointer that resolves to nothing in this bundle is kept rather than cleared. The
    trigger existed and fell outside this window or this subject, which is a fact about
    the bundle's edges and not about the cluster.
    """
    for finding in findings:
        target = finding.provenance.get(TRIGGER_FINGERPRINT)
        if target:
            finding.explains = str(target)


# ---- sources ---------------------------------------------------------------


def _source_reports(conn: Connection, start: datetime, end: datetime) -> list[SourceReport]:
    """Every expected source gets a row, including the ones that said nothing.

    A source missing from collection_runs is the case three-state status cannot
    express, because three-state describes an attempt and this is the absence of one.
    Left out, it reads to the reasoner as a source that simply had no findings.
    """
    seen = {r["source"]: r for r in reader.source_status_in_window(conn, start, end)}
    counts = {r["source"]: r for r in reader.run_counts_in_window(conn, start, end)}

    reports: list[SourceReport] = []
    for source in EXPECTED_SOURCES:
        row = seen.get(source)
        if row is None:
            reports.append(
                SourceReport(source=source, status=SourceStatus.UNAVAILABLE, never_ran=True)
            )
            continue

        tally = counts[source]
        reports.append(
            SourceReport(
                source=source,
                status=row["status"],
                last_started=parse(row["last_started"]),
                signals=row["signal_count"],
                error=row["error"],
                attempts=tally["attempts"],
                failed=tally["failed"],
            )
        )

    return reports


def _log_coverage(reports: list[SourceReport]) -> str | None:
    """Whether the log excerpts were chosen by sources that could see.

    k8s_logs reads only pods an upstream source reported, and proceeds on one healthy
    upstream by design. So a logs run reporting ok or empty while an upstream was blind
    is a partial result, and the collector cannot say so: it cannot know what the blind
    source would have reported. Only this layer holds every source's report at once.

    Unavailable logs are left alone. That is already stated in the source block, and
    there is no excerpt set to qualify. A source that never ran is reported unavailable
    by _source_reports, so that case is covered by the same check and needs no
    condition of its own.
    """
    by_source = {r.source: r for r in reports}
    logs = by_source.get(str(SignalSource.K8S_LOGS))
    if logs is None or logs.status == SourceStatus.UNAVAILABLE:
        return None

    blind = sorted(
        s for s in config.LOG_UPSTREAM_SOURCES
        if (r := by_source.get(s)) is None
        or r.never_ran
        or r.status == SourceStatus.UNAVAILABLE
        or r.failed > 0
    )
    if not blind:
        return None

    return (
        f"log excerpts were chosen from what {', '.join(blind)} reported, and "
        f"{'that source' if len(blind) == 1 else 'those sources'} could not see for "
        "the whole window; a failing pod only it would have named has no excerpt, so "
        "a missing excerpt is not evidence the application was fine"
    )


# ---- history, and what its absence costs -----------------------------------


HISTORY_DROPPED = (
    "history unavailable: cannot say whether any of these problems are new, "
    "when they were first seen, or whether they are happening in other clusters"
)


def _store_blind_note(cluster: str, watched: datetime | None, since: datetime) -> str | None:
    """Why newness cannot be judged, or None when it can.

    Strictly before: a store that began watching at the instant the window opened holds
    no row from before it, and would call everything new for exactly that reason.
    """
    if watched is not None and watched < since:
        return None
    if watched is None:
        return (
            f"the store holds nothing from {cluster}, so no finding can be called new "
            "or old"
        )
    return (
        f"the store has held {cluster} only since {watched.astimezone(UTC):%Y-%m-%d %H:%M} "
        "UTC, after this window began, so no finding can be called new: a problem older "
        "than the watching cannot be told from one that started inside it"
    )


def history_for(
    fingerprints: list[str], since: datetime, cluster: str
) -> tuple[dict[str, History], list[str]]:
    """Ask the store what the buffer cannot answer, and report it if it cannot answer.

    Imported here rather than at module scope so this package stays importable without
    the database driver. The store is history and is never on the critical path of a
    diagnosis: unreachable, the bundle loses its "is this new" claims and keeps
    everything else — and says which claims it lost, because a diagnosis made blind to
    history and one made with it are different verdicts against different inputs.
    """
    if not fingerprints:
        return {}, []

    try:
        from oncall import store

        found: dict[str, History] = {}
        with store.connect() as conn:
            watched = store.watching_since(conn, cluster)
            blind = _store_blind_note(cluster, watched, since)
            for fingerprint in fingerprints:
                found[fingerprint] = History(
                    first_seen_ever=store.first_seen_ever(conn, fingerprint),
                    is_new=None if blind else store.is_new(conn, fingerprint, since),
                    watching_since=watched,
                )
        return found, [blind] if blind else []
    except Exception:  # noqa: BLE001 - any failure to reach it costs the same claims
        return {}, [HISTORY_DROPPED]


# ---- promotion -------------------------------------------------------------
# The excerpt is what fits in a row; the blob is what the container actually wrote.
# Promotion spends a fixed byte budget buying back that loss, and spends it where the
# failure it explains ranks highest, because an excerpt is only as urgent as the thing
# that caused it to be read.


def _promotion_order(findings: list[Finding]) -> list[Finding]:
    """Findings that carry a blob, ordered by the rank of what they explain.

    A log excerpt's own severity describes the fetch, not the failure, so ranking
    excerpts by it would spend the budget on whichever read went most smoothly. The
    finding an excerpt explains is the one whose urgency it inherits. An excerpt whose
    trigger is outside the bundle still has text worth reading; it goes last.
    """
    position = {f.fingerprint: i for i, f in enumerate(findings)}
    unresolved = len(findings)
    carrying = [f for f in findings if f.blob_ids]
    return sorted(
        carrying,
        key=lambda f: position.get(f.explains, unresolved) if f.explains else unresolved,
    )


def promote(
    conn: Connection, findings: list[Finding], budget: int | None = None
) -> list[Promotion]:
    """Unabridged text for the excerpts that matter most, until the budget runs out.

    Whole blobs or nothing. Cutting a blob to fit would manufacture a second excerpt with
    its own unstated edges, which is the loss this exists to buy back. A blob that does
    not fit is recorded with its size, so the reader knows the full text existed and was
    left on disk rather than never collected.

    Only the newest blob per finding. Older ones cover earlier windows of the same
    problem, and the newest is the one the excerpt in facts was cut from.

    read_blob never raises, and neither does this. Every outcome is a Promotion, so a
    blob retention removed and a blob that failed its digest are both on the record
    rather than both silently absent.
    """
    remaining = config.BUNDLE_PROMOTE_BYTES if budget is None else budget
    out: list[Promotion] = []

    for finding in _promotion_order(findings):
        blob_id = finding.blob_ids[-1]
        blob = blobs.read_blob(conn, blob_id)

        # read_blob returns data only with OK, so no bytes is every other outcome.
        if blob.data is None:
            out.append(
                Promotion(
                    fingerprint=finding.fingerprint,
                    blob_id=blob_id,
                    status=blob.status,
                    reason=f"blob {blob.status}; the excerpt is all that remains",
                )
            )
            continue

        size = len(blob.data)
        if size > remaining:
            out.append(
                Promotion(
                    fingerprint=finding.fingerprint,
                    blob_id=blob_id,
                    status=blob.status,
                    bytes=size,
                    reason=f"over budget: {size} bytes, {remaining} left",
                )
            )
            continue

        remaining -= size
        out.append(
            Promotion(
                fingerprint=finding.fingerprint,
                blob_id=blob_id,
                status=blob.status,
                bytes=size,
                text=blob.data.decode("utf-8", errors="replace"),
                reason="promoted",
            )
        )
    return out


# ---- the window ------------------------------------------------------------


def derive_window(
    conn: Connection,
    cluster: str,
    now: datetime,
    subject_name: str | None = None,
) -> tuple[datetime, datetime, str]:
    """Start from a fixed lookback, then reach back to where the leading problem began.

    A window chosen before the evidence is read describes the alert, not the incident.
    A crash that has been happening for three hours inside a one-hour window looks like
    it started an hour ago, and "started an hour ago" is the single most misleading
    thing a bundle can say, because it invites a correlation with whatever else
    happened an hour ago.

    Two passes rather than one clever query: rank the default window, take the leading
    finding, and ask when that fingerprint first occurred within the cap. The cap is
    not a performance bound — it is the buffer's retention horizon. Extending past it
    would silently produce a window over whatever survived the sweep, and report the
    sweep's edge as the problem's beginning.
    """
    end = now
    start = now - timedelta(seconds=config.BUNDLE_LOOKBACK_SECONDS)
    floor = now - timedelta(seconds=config.BUNDLE_MAX_LOOKBACK_SECONDS)

    provisional = build(
        conn, cluster, start, end, subject_name=subject_name, history=False, promotion=False
    )
    if not provisional.findings:
        return start, end, WINDOW_FIXED

    leading = provisional.findings[0]

    # Asked from the floor, this query can never answer "older than the floor" — the
    # rows that would say so are the ones it excludes, so the cap silently becomes
    # unreachable and a week-old problem is reported as a fresh one. The cap bounds
    # the window that gets offered, not the question that gets asked; a bound on the
    # fetch quietly becoming a bound on what can be asserted is the same mistake as
    # trimming a log and then reporting no errors in it.
    #
    # Unbounded below is affordable because the lookup is by fingerprint and the
    # buffer holds two days: the scan is over one problem's rows in a small file.
    times = reader.occurrence_times(conn, leading.fingerprint, EPOCH, end)
    if not times:
        return start, end, WINDOW_FIXED

    # min() over the strings rather than over parsed datetimes: iso() is fixed width
    # by design precisely so lexicographic order is chronological order.
    earliest = parse(min(times))
    if earliest >= start:
        return start, end, WINDOW_FIXED

    if earliest < floor:
        return floor, end, WINDOW_CAPPED
    return earliest, end, WINDOW_EXTENDED


# ---- assembly --------------------------------------------------------------


def build(
    conn: Connection,
    cluster: str,
    start: datetime,
    end: datetime,
    subject_name: str | None = None,
    window_basis: str = WINDOW_FIXED,
    history: bool = True,
    max_findings: int | None = None,
    promotion: bool = True,
) -> Bundle:
    """Everything known about one subject over one window, collapsed, ranked and capped.

    Selection happens after ranking and records what it left out. The cap is a prompt
    budget and nothing more, which is why the omissions carry occurrence counts: forty
    findings dropped and none dropped are different inputs, and the reasoner has to be
    able to tell which one it was given.
    """
    rows = reader.signals_in_window(conn, cluster=cluster, start=start, end=end)

    degraded: list[str] = []
    scope: Scope | None = None
    if subject_name is None:
        joins = {r["signal_id"]: JOIN_CLUSTER for r in rows}
    else:
        scope = resolve(rows, subject_name)
        rows, joins = select(rows, scope)
        if not scope.found:
            degraded.append(
                f"{subject_name} produced no signal in this window, so nothing could "
                "be scoped to it; an empty bundle here says nothing about its health"
            )

    findings = [_finding_from(group, start) for group in _group_by_fingerprint(rows)]
    for finding in findings:
        finding.joined_by = strongest(joins[i] for i in finding.signal_ids)
    _link_triggers(findings)
    findings.sort(key=_order, reverse=True)

    cap = config.BUNDLE_MAX_FINDINGS if max_findings is None else max_findings
    kept, dropped = findings[:cap], findings[cap:]

    if history:
        found, notes = history_for([f.fingerprint for f in kept], start, cluster)
        degraded.extend(notes)
        for finding in kept:
            finding.history = found.get(finding.fingerprint)

    versions = sorted({str(r["normalizer_version"]) for r in rows if r["normalizer_version"]})
    if len(versions) > 1:
        degraded.append(
            "fingerprints in this window were produced by more than one normalizer "
            f"version ({', '.join(versions)}); recurrence and first-seen answers "
            "describe the keying rules as much as the cluster"
        )

    sources = _source_reports(conn, start, end)
    coverage = _log_coverage(sources)
    if coverage:
        degraded.append(coverage)

    losses = [
        Loss(
            reason=r["reason"],
            rows_dropped=r["rows_dropped"],
            unshipped=r["unshipped"],
            window_start=r["window_start"],
            window_end=r["window_end"],
        )
        for r in reader.buffer_drops_in_window(conn, start, end)
    ]

    return Bundle(
        cluster=cluster,
        subject=subject_name,
        window_start=start,
        window_end=end,
        window_basis=window_basis,
        scope=scope,
        findings=kept,
        promoted=promote(conn, kept) if promotion else [],
        sources=sources,
        losses=losses,
        omitted=[
            Omission(
                fingerprint=f.fingerprint,
                source=str(f.source),
                severity=str(f.severity) if f.severity else None,
                occurrences=f.occurrences,
                reason=f"below the top {cap} by rank",
            )
            for f in dropped
        ],
        candidate_signal_ids=[r["signal_id"] for r in rows],
        degraded=degraded,
        normalizer_versions=versions,
    )


def assemble(
    conn: Connection,
    cluster: str,
    subject_name: str | None = None,
    now: datetime | None = None,
) -> Bundle:
    """The entry point a caller should use: derive the window, then fill it."""
    moment = now or datetime.now(UTC)
    start, end, basis = derive_window(conn, cluster, moment, subject_name)
    return build(conn, cluster, start, end, subject_name=subject_name, window_basis=basis)


# ---- receipts --------------------------------------------------------------
# Two hashes, because one cannot carry two promises. Reword a heading in the renderer
# and a prompt receipt churns, so no diagnosis recorded before the edit is comparable
# to one recorded after it even though the evidence was identical. Hash only the
# evidence and a template that silently dropped a section leaves the receipts matching
# while the model saw less, and the regression is attributed to the model.


def receipt_basis(bundle: Bundle) -> dict[str, Any]:
    """Exactly what bundle_sha256 promises about, and nothing else.

    In: the evidence, and the absence facts the reasoner was shown and had to account
    for — which sources were down, what selection left out, which claims were dropped,
    and under which keying rules identity was computed. A diagnosis made blind to
    history and one made with it are different verdicts against different inputs, and
    the eval corpus has to be able to tell them apart.

    Out: provenance, run identifiers, collection timestamps, and the count of signals
    a source's last run happened to produce. Those describe the observing rather than
    the observation, and folding them in would move the hash on every cycle — leaving
    the corpus with no two diagnoses that share an input, which is exactly as useless
    as a hash that never moves.

    Findings are ordered by fingerprint rather than by rank. Order is what the model
    reads and therefore belongs to the prompt receipt; changing a ranking rule changes
    the prompt, and it does not change which evidence was selected.
    """
    return {
        "cluster": bundle.cluster,
        "subject": bundle.subject,
        # The basis and not the bounds. The bounds come off the clock, so hashing them
        # gave two assemblies a minute apart over identical evidence different receipts
        # (found live on 2026-09-29). The window is the question; what it admitted is
        # already here as findings, omissions and source statuses.
        "window_basis": bundle.window_basis,
        "scope": bundle.scope.model_dump(mode="json") if bundle.scope else None,
        "normalizer_versions": bundle.normalizer_versions,
        "degraded": sorted(bundle.degraded),
        "sources": sorted(
            [[s.source, str(s.status), s.never_ran, s.failed > 0] for s in bundle.sources]
        ),

        # blob_id is a content hash, so it identifies the text without carrying it.
        "promoted": sorted(
            [[p.fingerprint, p.blob_id, p.status, p.bytes, p.text is not None]
             for p in bundle.promoted]
        ),
        "omitted": sorted(
            [[o.fingerprint, o.source, o.occurrences, o.reason] for o in bundle.omitted]
        ),
        "losses": sorted(
            [[loss.reason, loss.rows_dropped, loss.unshipped, loss.window_start,
              loss.window_end] for loss in bundle.losses]
        ),
        "findings": [
            {
                "fingerprint": f.fingerprint,
                "source": str(f.source),
                "kind": str(f.kind),
                "severity": str(f.severity) if f.severity else None,
                "severity_basis": f.severity_basis,
                "subject": f.subject_name,
                "owner": f.owner_name,
                "node": f.node_name,
                "occurrences": f.occurrences,
                "first_seen": iso(f.first_seen),
                "last_seen": iso(f.last_seen),
                "facts": f.facts,
                "trends": f.trends,
                "partial": f.partial,
                "sample": f.sample,
                "deltas": [d.model_dump(mode="json") for d in f.deltas],
                "explains": f.explains,
                "joined_by": f.joined_by,
                "observational": f.observational,
                "history": f.history.model_dump(mode="json") if f.history else None,
            }
            for f in sorted(bundle.findings, key=lambda f: f.fingerprint)
        ],
    }


def evidence_receipt(bundle: Bundle) -> str:
    """Which evidence was selected. Survives a reworded template on purpose."""
    return hashlib.sha256(
        json.dumps(receipt_basis(bundle), sort_keys=True, default=str).encode()
    ).hexdigest()


def prompt_receipt(text: str) -> str:
    """Which bytes reached the model. Moves whenever the rendering moves, on purpose."""
    return hashlib.sha256(text.encode()).hexdigest()


# ---- inspection ------------------------------------------------------------
# A human-readable view for judging output by eye. Not the prompt: what the reasoner
# is shown is M5's decision, and binding the two now would mean every wording change
# rewrote the artefact the evidence receipt is supposed to be independent of.


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
        f"  [{bundle.window_start:%H:%M} → {bundle.window_end:%H:%M}]  ({bundle.window_basis})",
        f"{len(bundle.findings)} findings from {bundle.signals_read} signals",
        "",
    ]

    for f in bundle.findings:
        span = f"{f.first_seen:%H:%M} → {f.last_seen:%H:%M}"
        seen_before = ""
        if f.history and f.history.is_new is not None:
            seen_before = "  NEW" if f.history.is_new else f"  since {f.history.first_seen_ever:%b %d}"
        out.append(
            f"[{str(f.severity or '-'):8}] {f.source:12} x{f.occurrences:<3} {span}"
            f"  {f.subject_name or ''}  via {f.joined_by}{seen_before}"
        )

        if f.severity_basis == BASIS_CLUSTER:
            out.append("      (severity is Kubernetes' own Normal/Warning label, not an assessment)")
        if f.observational:
            out.append("      (reports on what could be seen, not on the workload)")
        if f.explains:
            trigger = by_fingerprint.get(f.explains)
            out.append(
                f"      explains: {label(trigger)}" if trigger
                else "      explains: a trigger outside this window"
            )

        gap = " (intermittent)"
        counted = {d.key for d in f.deltas}
        for key, value in f.facts.items():
            if key in counted:
                continue
            out.append(f"      {key} = {clip(value)}{gap if key in f.partial else ''}")
        for key, (first, last) in f.trends.items():
            if key in counted:
                continue
            # A key is only in trends because it moved, so equal endpoints mean it
            # moved and came back. Saying so is the difference between a reader seeing
            # a flap and a reader seeing a broken diff.
            detour = "  (varied in between)" if first == last else ""
            out.append(
                f"      {key} : {clip(first)}  →  {clip(last)}{detour}"
                f"{gap if key in f.partial else ''}"
            )
        for d in f.deltas:
            change = f"{d.change:+d}" if d.change is not None else "change unknown"
            out.append(f"      {d.key} : {d.first} → {d.last}  ({change} — {d.basis})")
        if f.sample:
            out.append(f"      e.g. {clip(f.sample)}")
        out.append("")

    out.append("sources")
    for s in bundle.sources:
        note = "never ran" if s.never_ran else f"{s.signals} signals"
        if s.failed:
            note += f", {s.failed} of {s.attempts} attempts failed"

        out.append(
            f"      {s.source:12} {s.status:12} {note}{'  ' + s.error if s.error else ''}"
        )

    if bundle.promoted:
        out += ["", f"promoted ({len(bundle.promoted)})"]
        for p in bundle.promoted:
            target = by_fingerprint.get(p.fingerprint)
            named = label(target) if target else p.fingerprint
            out.append(f"      {named}: {p.reason}")
            if p.text:
                out += [f"        | {line}" for line in p.text.splitlines()[-20:]]

    if bundle.omitted:
        out += ["", f"omitted ({len(bundle.omitted)})"]
        for o in bundle.omitted:
            out.append(f"      {o.source:12} {str(o.severity):8} x{o.occurrences:<4} {o.reason}")

    if bundle.losses:
        out += ["", f"buffer losses ({len(bundle.losses)})"]
        for loss in bundle.losses:
            out.append(
                f"      {loss.rows_dropped} rows dropped ({loss.reason}), "
                f"{loss.unshipped} never shipped, "
                f"{loss.window_start} to {loss.window_end}"
            )

    if bundle.degraded:
        out += ["", "degraded"]
        out += [f"      {note}" for note in bundle.degraded]

    return "\n".join(out)
