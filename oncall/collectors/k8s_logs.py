"""
Log excerpt collector.

  - Signal-driven, not a sweep. Every other collector asks the whole cluster; this one
    asks only the pods something else has already reported as broken. Log bytes are
    the most expensive thing the agent handles, and the choice of targets — not any
    per-fetch bound — is where that cost is actually controlled.
  - That makes it the first source whose visibility depends on another source. Status
    is a claim about what this collector could see, so it consults collection_runs
    before it is allowed to say 'empty': no targets because the cluster is healthy and
    no targets because pod state was unavailable are the same input and opposite facts.
  - The window is chosen by time and bounded by bytes. since_seconds gives an interval
    with known edges, which is what lets an excerpt join to the event that explains it;
    tail_lines returns a span of unknown duration and cannot participate in that join.
    The byte cap is the backstop, because since_seconds bounds time and not volume.
  - Nothing is parsed out of the log text. event_time and severity come from the signal
    that triggered the fetch; only covered_through comes from the stream, and from the
    runtime's own per-line timestamp rather than from anything the application wrote.
  - The excerpt is lossy by construction — trimmed, and repeated lines collapsed. The
    blob is not. That division is the whole reason the blob path was built first.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from kubernetes.client.exceptions import ApiException

from oncall import config
from oncall import landing_zone as lz
from oncall.collectors import k8s_client as k8s
from oncall.envelope import (
    Owner,
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    iso,
    parse,
    partition_payload,
    redact,
    template_of,
)

STREAM_PREVIOUS = "previous"
# The two halves of what this row points at rather than states.
#
# window_start and window_end come off the clock and describe the interval that was
# requested; covered_through is derived from the runtime's own line timestamps and
# describes what came back. Only the second is evidence, and it is the cheaper of
# the two by a factor of seven. The requested window stays here because the overlap
# test that correlates this excerpt with anything else is computed from it.
#
# trigger_* is the join back to the failure that caused the read. A consumer should
# render the relationship, never the hex: trigger_signal_id takes a new value on
# every poll by construction, so shown verbatim it is the noisiest field in the
# payload and says nothing a reader can act on.
PROVENANCE_KEYS = frozenset({
    "window_start",
    "window_end",
    "trigger_source",
    "trigger_fingerprint",
    "trigger_signal_id",
})


STREAM_CURRENT = "current"

# Sources that can put a Pod-subject signal in front of this collector. Named rather
# than inferred, because the health check below has to know which runs to look for and
# an unlisted source would silently stop counting as upstream.
UPSTREAM = (SignalSource.K8S_PODS, SignalSource.K8S_EVENTS)

# Ranking used only to choose which trigger represents a target when several land in
# one window. It never reaches a stored field — severity on the emitted signal is
# inherited verbatim from the trigger.
_SEVERITY_RANK = {
    str(Severity.WARNING): 1,
    str(Severity.ERROR): 2,
    str(Severity.CRITICAL): 3,
}

# The runtime's timestamp prefix, present because the fetch asks for timestamps=True.
# Written by the container runtime, not by the application, which is what makes it safe
# to read: the rule against parsing time out of log text is about the application's own
# formatting, which is arbitrary and whose misreading would be silent.
_TS_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s(.*)$", re.S)


# ---- targets ---------------------------------------------------------------


class Target(NamedTuple):
    namespace: str | None
    pod: str
    uid: str | None
    container: str | None
    streams: tuple[str, ...]
    node: str | None
    owner_kind: str | None
    owner_name: str | None
    severity: str | None
    event_time: str
    trigger_source: str
    trigger_fingerprint: str
    trigger_signal_id: str


def _payload_of(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["payload"]) or {}
    except (TypeError, ValueError):
        return {}


def _streams_for(payload: dict[str, Any], source: str) -> tuple[str, ...]:
    """Which log stream holds the evidence.

    A container in CrashLoopBackOff is not running, so its current stream is empty or a
    few lines of an attempt that has not failed yet; the trace is in the container that
    already died. A container that is running now but has restarted is the inverse trap
    from k8s_pods — every live indicator reads healthy and the previous container's log
    is the only evidence, so both streams are worth having.

    Guessing wrong here is cheap in a way that guessing wrong about a stored key is
    not: this decision shapes a fetch, and a fetch that comes back empty or 400s is
    recorded as such and can be made again. That asymmetry is why a reason-shaped
    heuristic is acceptable here and refused in dedupe_key.
    """
    if source == str(SignalSource.K8S_PODS):
        restarted = bool(payload.get("restart_count"))
        terminated = payload.get("exit_code") is not None
        backing_off = bool(payload.get("waiting_reason"))

        if backing_off or terminated:
            return (STREAM_PREVIOUS,)
        if restarted:
            return (STREAM_PREVIOUS, STREAM_CURRENT)
        return (STREAM_CURRENT,)

    # An event names a condition, not a container state, so there is nothing to read
    # the answer off. Previous is asked for first because the events that reach this
    # severity are overwhelmingly about a container that has already stopped; the fetch
    # falls back to current when there is no previous container.
    return (STREAM_PREVIOUS,)


def targets_from_rows(rows: list[Any], limit: int) -> tuple[list[Target], int]:
    """Collapse trigger signals to one target per pod, container and stream.

    Returns the targets and how many distinct ones were declined by the cap. The count
    is returned rather than logged and forgotten: a run that quietly stopped at forty
    pods looks exactly like a cluster with forty broken pods, and the difference is the
    whole diagnosis during a cascading failure.
    """
    best: dict[tuple[Any, ...], Target] = {}

    for row in rows:
        payload = _payload_of(row)
        container = payload.get("container") or None

        for stream in _streams_for(payload, row["source"]):
            key = (row["namespace"], row["subject_name"], container, stream)
            candidate = Target(
                namespace=row["namespace"],
                pod=row["subject_name"],
                uid=row["subject_uid"],
                container=container,
                streams=(stream,),
                node=row["node_name"],
                owner_kind=row["owner_kind"],
                owner_name=row["owner_name"],
                severity=row["severity"],
                event_time=row["event_time"],
                trigger_source=row["source"],
                trigger_fingerprint=row["fingerprint"],
                trigger_signal_id=row["signal_id"],
            )

            # Rows arrive newest first, so the first sighting of a key is already the
            # most recent. It is replaced only by something more severe, which keeps
            # the excerpt attached to the worst thing that happened rather than the
            # last thing.
            held = best.get(key)
            if held is None or _rank(candidate.severity) > _rank(held.severity):
                best[key] = candidate

    # Two stable passes rather than one composite key: newest first, then most severe
    # first. Python's sort is stable, so the second pass preserves the first's order
    # within a severity — which is what "worst, and among equals the freshest" means.
    ordered = sorted(best.values(), key=lambda t: t.event_time, reverse=True)
    ordered.sort(key=lambda t: -_rank(t.severity))

    return ordered[:limit], max(0, len(ordered) - limit)


def _rank(severity: str | None) -> int:
    return _SEVERITY_RANK.get(severity or "", 0)


# ---- the excerpt -----------------------------------------------------------


class Excerpt(NamedTuple):
    text: str
    lines_seen: int
    lines_kept: int
    collapsed: bool
    redacted: bool
    covered_through: str | None


def parse_lines(text: str) -> list[tuple[str | None, str]]:
    """Split the stream into (runtime timestamp, body) pairs.

    A line without a parseable prefix keeps a None timestamp rather than being dropped
    or given a guessed one — multi-line stack traces arrive as continuation lines with
    no prefix of their own, and they are the most useful part of the fetch.
    """
    out: list[tuple[str | None, str]] = []
    for line in text.splitlines():
        match = _TS_PREFIX.match(line)
        out.append((match.group(1), match.group(2)) if match else (None, line))
    return out


def build_excerpt(
    text: str,
    max_lines: int | None = None,
    max_bytes: int | None = None,
) -> Excerpt:
    """Redact, collapse repeats by template, then keep the tail.

    Collapsing runs before trimming, so four hundred identical lines cannot consume the
    whole budget and push the one interesting line out of the window. It uses the same
    templating as identity does, which is the second job that capability was built for.

    The tail rather than the head: the fetch streams oldest-first and the byte cap cuts
    the newest lines, so whatever survives has its most recent end nearest the failure.

    This is lossy on purpose — interleaving between collapsed groups is destroyed, and
    the trim discards the rest. The unabridged stream is in the blob, which is why the
    blob is written before this runs and not derived from it.
    """
    lines = max_lines if max_lines is not None else config.LOG_EXCERPT_LINES
    budget = max_bytes if max_bytes is not None else config.LOG_EXCERPT_BYTES

    parsed = parse_lines(text)
    covered_through = next(
        (ts for ts, _ in reversed(parsed) if ts is not None), None
    )

    kept: list[list[Any]] = []
    seen: dict[str, int] = {}
    any_redacted = False
    collapsed = False

    for ts, body in parsed:
        clean, was_redacted = redact(body)
        any_redacted = any_redacted or was_redacted

        key = template_of(clean).key
        if key and key in seen:
            kept[seen[key]][2] += 1
            collapsed = True
            continue

        seen[key] = len(kept)
        kept.append([ts, clean, 1])

    tail = kept[-lines:]
    rendered = "\n".join(
        f"{ts + ' ' if ts else ''}{body}" + (f"  (x{count})" if count > 1 else "")
        for ts, body, count in tail
    )

    # Trimmed from the front for the same reason the tail is kept: the end of the
    # window is the end nearest the failure.
    encoded = rendered.encode()
    if len(encoded) > budget:
        rendered = encoded[-budget:].decode(errors="ignore")

    return Excerpt(
        text=rendered,
        lines_seen=len(parsed),
        lines_kept=len(tail),
        collapsed=collapsed,
        redacted=any_redacted,
        covered_through=covered_through,
    )


# ---- signal construction ---------------------------------------------------


class Fetch(NamedTuple):
    """text is what the excerpt is built from; raw is what the blob stores.

    Both, and not one derived from the other at the point of use. blob_id is the sha256
    of the bytes, so hashing a re-encoding of a lossily decoded string would address a
    sanitised copy rather than what the container actually emitted — and two different
    malformed logs could then collapse onto one blob.

    raw carries a default so a caller that only cares about the decoded form cannot
    accidentally construct a Fetch that stores an empty blob beside a non-empty excerpt.
    """

    status: str
    text: str
    stream: str
    truncated: bool
    fell_back: bool
    error: str | None
    raw: bytes = b""


def _dedupe_key(container: str | None, stream: str) -> str:
    """container|stream.

    Not the content, and not a template of it. A log excerpt is corroborating evidence
    attached to an occurrence of a problem, not a problem in its own right — the thing
    being ranked and counted is the pod-state or event signal that triggered the fetch,
    and keying on log content would enter the same failure into recurrences() twice
    under two identities.

    Paired with event_time inherited from the trigger, this also makes the write
    idempotent: re-running a cycle over the same trigger produces the same signal_id.
    """
    return f"{container or ''}|{stream}"


def build_signal(
    target: Target,
    fetch: Fetch,
    excerpt: Excerpt | None,
    blob_id: str | None,
    cluster: str,
    window_start: datetime,
    window_end: datetime,
) -> Signal:
    payload: dict[str, Any] = {
        "container": target.container,
        "stream": fetch.stream,
        # Three-state, one level down from the collection run. A pod whose logs could
        # not be read must not produce the same row as a pod whose logs were empty:
        # the second says the container printed nothing, the first says nothing is
        # known — and a reasoner cannot tell them apart from an absent excerpt.
        "log_status": fetch.status,
        "error": fetch.error,
        # The interval that was asked for, and the last moment actually covered by a
        # line. Without covered_through a truncated fetch tells the reasoner that
        # something is missing but not where, and a gap of unknown position is barely
        # better than no warning at all.
        "window_start": iso(window_start),
        "window_end": iso(window_end),
        "covered_through": excerpt.covered_through if excerpt else None,
        "truncated": fetch.truncated or None,
        "lines_seen": excerpt.lines_seen if excerpt else None,
        "lines_kept": excerpt.lines_kept if excerpt else None,
        "collapsed": (excerpt.collapsed or None) if excerpt else None,
        "excerpt": excerpt.text if excerpt else None,
        # The previous container did not exist. A fact about the target, not a failure
        # to see it — the fetch fell back to the running container and said so.
        "previous_unavailable": fetch.fell_back or None,
        # What made us look. This is the join that ties an excerpt to the failure it
        # explains; without it the assembler has a log and a crash in the same window
        # and no stated relationship between them.
        "trigger_source": target.trigger_source,
        "trigger_fingerprint": target.trigger_fingerprint,
        "trigger_signal_id": target.trigger_signal_id,
    }

    return Signal(
        source=SignalSource.K8S_LOGS,
        kind=SignalKind.LOG_EXCERPT,
        cluster=cluster,
        # From the trigger, never from the stream. The excerpt belongs to the moment
        # the failure happened; when we managed to read it is collected_at, and the two
        # are kept apart for the same reason they were in M1.
        event_time=parse(target.event_time),
        namespace=target.namespace,
        subject=Subject(kind="Pod", name=target.pod, uid=target.uid),
        owner=(
            Owner(kind=target.owner_kind, name=target.owner_name)
            if target.owner_kind and target.owner_name
            else None
        ),
        node=target.node,
        # Inherited, not derived. Reading a level out of arbitrary log formatting would
        # make an unfamiliar format read as less serious than a familiar one, which is
        # the escalation-list mistake with worse input.
        severity=Severity(target.severity) if target.severity else None,
        dedupe_key=_dedupe_key(target.container, fetch.stream),
        redacted=bool(excerpt and excerpt.redacted),
        blob_id=blob_id,
        payload=partition_payload(payload, PROVENANCE_KEYS),
    )


# ---- I/O -------------------------------------------------------------------


def _read_log(target: Target, stream: str) -> Fetch:
    """One log read, with every failure kept local to the target.

    A missing previous container is a 400 and is a fact about this pod, so it falls
    back to the running stream and records that it did. Anything else is a failure to
    see, and is recorded against this target alone: one unreadable pod must not turn
    the whole source unavailable, because the other thirty-nine targets are real
    evidence and withholding them helps nobody.

    The body is taken unparsed. A log endpoint is declared as returning a string, and
    the generated client turns the response into one by calling str() on its bytes
    rather than decoding them — so the caller receives the repr of a bytes object,
    newlines included as their two-character escape. Nothing raises: the value is a
    str, it is non-empty, and every mechanism downstream keeps working on it. Line
    splitting then finds one line, the runtime timestamp prefix never matches, and an
    empty log arrives as the four characters b'' and is recorded as ok with content.
    Owning the decode is what removes all of that, and it is only visible from outside
    the process — a stub that returns a real string reproduces none of it.
    """
    want_previous = stream == STREAM_PREVIOUS

    for attempt, previous in enumerate((want_previous, False) if want_previous else (False,)):
        try:
            resp = k8s.core_v1().read_namespaced_pod_log(
                name=target.pod,
                namespace=target.namespace,
                container=target.container,
                previous=previous,
                since_seconds=config.LOG_SINCE_SECONDS,
                limit_bytes=config.LOG_LIMIT_BYTES,
                timestamps=True,
                _request_timeout=k8s.REQUEST_TIMEOUT,
                _preload_content=False,
            )
        except ApiException as exc:
            if exc.status == 400 and previous:
                # No previous container. Fall through to the running one.
                continue
            return Fetch(
                str(SourceStatus.UNAVAILABLE), "", stream, False, False,
                f"ApiException {exc.status}: {exc.reason}",
            )
        except Exception as exc:  # noqa: BLE001 - any failure to read means the same
            return Fetch(
                str(SourceStatus.UNAVAILABLE), "", stream, False, False,
                f"{type(exc).__name__}: {exc}",
            )

        raw: bytes = resp.data or b""

        # replace rather than strict. A container emits whatever bytes it likes, and a
        # decoding error would end the cycle for every target after this one — the same
        # blast radius the per-target error handling above exists to prevent.
        text = raw.decode("utf-8", errors="replace")
        used = STREAM_PREVIOUS if previous else STREAM_CURRENT

        # The API gives no truncation flag, so this is inferred from hitting the cap.
        # Measured on the bytes the server sent, which is the unit limit_bytes bounds;
        # measuring the decoded string would count replacement characters as though
        # they were the bytes they stand in for.
        #
        # It can over-claim when a log is exactly the cap long, and that is the right
        # direction to be wrong in: over-claiming costs a caveat, under-claiming lets
        # the reasoner state that there were no errors in the logs.
        truncated = len(raw) >= config.LOG_LIMIT_BYTES

        return Fetch(
            str(SourceStatus.OK if text.strip() else SourceStatus.EMPTY),
            text,
            used,
            truncated,
            bool(attempt),
            None,
            raw,
        )

    return Fetch(str(SourceStatus.EMPTY), "", STREAM_CURRENT, False, True, None)


def _upstream_verdict(statuses: list[Any]) -> str | None:
    """None when at least one feeding source was able to look. Otherwise the reason
    this collector is blind rather than idle.

    This is the whole cost of being signal-driven, paid explicitly. No targets because
    nothing is broken and no targets because pod state never ran are the same input,
    and a collector that reports 'empty' for both has produced the most confident
    possible statement of health out of an absence of information.
    """
    seen = {row["source"]: row["status"] for row in statuses if row["source"] in
            {str(s) for s in UPSTREAM}}

    if not seen:
        return "no upstream collection run in window; targets cannot be determined"

    if all(status == str(SourceStatus.UNAVAILABLE) for status in seen.values()):
        return f"every upstream source unavailable in window: {sorted(seen)}"

    return None


def collect(cluster: str) -> tuple[SourceStatus, list[Signal], str | None]:
    """Returns (status, signals, error).

    Blobs are written in their own transaction, before the signals that point at them
    are handed back for run_once to write. A crash in between leaves blob rows nothing
    references, which the age sweep reclaims — the same designed wreckage as everywhere
    else on this path, and the reason the order is not the other way round.

    error is populated on an ok run when the target cap bit. A non-null error beside a
    healthy status is the channel for "this completed, with something you need to know"
    — and a cap that says nothing reads downstream as a complete picture.
    """
    end = datetime.now(UTC)
    start = end - timedelta(seconds=config.LOG_LOOKBACK_SECONDS)

    try:
        with lz.connect() as conn:
            verdict = _upstream_verdict(lz.source_status_in_window(conn, start, end))
            if verdict:
                return SourceStatus.UNAVAILABLE, [], verdict

            rows = lz.log_targets(
                conn, cluster, start, end, tuple(config.LOG_TRIGGER_SEVERITIES)
            )
    except Exception as exc:  # noqa: BLE001
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"

    targets, declined = targets_from_rows(
        [r for r in rows if k8s.in_scope(r["namespace"])], config.LOG_MAX_TARGETS
    )

    fetched = [(t, _read_log(t, t.streams[0])) for t in targets]

    signals: list[Signal] = []
    try:
        with lz.connect() as conn:
            for target, fetch in fetched:
                excerpt = build_excerpt(fetch.text) if fetch.text else None
                # Keyed off raw rather than text, so the condition and the stored bytes
                # are the same value. Deciding on the decoded form would let a Fetch
                # carrying an excerpt but no bytes write an empty blob under a real id.
                blob_id = (
                    lz.put_blob(conn, fetch.raw) if fetch.raw.strip() else None
                )
                signals.append(
                    build_signal(target, fetch, excerpt, blob_id, cluster, start, end)
                )
    except Exception as exc:  # noqa: BLE001
        return SourceStatus.UNAVAILABLE, [], f"{type(exc).__name__}: {exc}"

    note = (
        f"target cap reached: {declined} more pods matched than were collected"
        if declined
        else None
    )
    return (SourceStatus.OK if signals else SourceStatus.EMPTY), signals, note
