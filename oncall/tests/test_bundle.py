"""
Assembler tests: rows in, one bounded picture out.

  - The buffer is the input, not a stub. Every case writes real signals through the
    real writer and reads them back through build(), because the assembler's whole job
    is a shape transformation over rows and a fake row proves nothing about it.
  - The facts/trends split gets the most attention. It is the one rule here that is
    inferred rather than declared, and the presence-versus-value distinction inside it
    was wrong on the first attempt in a way that looked like a rendering bug.
  - Provenance is asserted by absence. A key the collector marked must not reach facts
    or trends by any route, and the assertion has to be that it is missing rather than
    that some filter ran.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    iso,
    partition_payload,
    provenance_of,
    visible,
)
from oncall.evidence import bundle

CLUSTER = "test-cluster"
POD = "api-7f9"

# Anchored near real time because source reports are read from collection_runs, whose
# started_at is stamped by the writer at call time. A window in the distant past would
# report every source as never having run, which is a case worth testing and a poor
# default for the ones that are not about it.
NOW = datetime.now(UTC).replace(microsecond=0)
START = NOW - timedelta(minutes=30)
END = NOW + timedelta(minutes=1)


# ---- helpers ---------------------------------------------------------------


def make_signal(
    *,
    when: datetime,
    payload: dict | None = None,
    source: SignalSource = SignalSource.K8S_PODS,
    kind: SignalKind = SignalKind.POD_STATE,
    key: str = "app|Error|1",
    severity: Severity | None = Severity.ERROR,
    pod: str = POD,
) -> Signal:
    return Signal(
        source=source,
        kind=kind,
        cluster=CLUSTER,
        event_time=when,
        namespace="prod",
        subject=Subject(kind="Pod", name=pod, uid="uid-1"),
        severity=severity,
        dedupe_key=key,
        payload=payload or {},
    )


def store(signals: list[Signal], source: SignalSource = SignalSource.K8S_PODS) -> None:
    with lz.connect() as conn:
        run = lz.start_run(conn, source, CLUSTER)
        lz.write_signals(conn, run, signals)
        lz.finish_run(conn, run, SourceStatus.OK, len(signals))


def build(subject: str | None = POD, **kwargs) -> bundle.Bundle:
    """History is off unless a case asks for it, so the suite never depends on a
    reachable store to exercise anything that is not about history."""
    kwargs.setdefault("history", False)
    with lz.connect() as conn:
        return bundle.build(conn, CLUSTER, START, END, subject_name=subject, **kwargs)


def marked(payload: dict, *provenance: str) -> dict:
    return partition_payload(payload, frozenset(provenance))


# ---- the payload contract --------------------------------------------------


def test_provenance_is_partitioned_and_unset_keys_are_dropped():
    stored = partition_payload(
        {"reason": "BackOff", "count": 67, "event_uid": "u-1", "absent": None},
        frozenset({"event_uid"}),
    )

    assert visible(stored) == {"reason": "BackOff", "count": 67}
    assert provenance_of(stored) == {"event_uid": "u-1"}
    assert "absent" not in stored


def test_a_row_written_before_provenance_existed_reads_as_all_evidence():
    """The compatibility claim the whole change rests on. Old rows carry a flat payload
    and must render exactly as they always did, which means no migration and no
    version check anywhere in the read path."""
    flat = {"reason": "BackOff", "event_uid": "u-1"}

    assert provenance_of(flat) == {}
    assert visible(flat) == flat


def test_a_provenance_key_never_reaches_facts_or_trends(buffer):
    marked = partition_payload(
        {"reason": "Error", "trigger_signal_id": "s" * 32}, frozenset({"trigger_signal_id"})
    )
    store([make_signal(when=NOW - timedelta(minutes=n), payload=marked) for n in (5, 3)])

    finding = build().findings[0]

    assert finding.facts["reason"] == "Error"
    assert "trigger_signal_id" not in finding.facts
    assert "trigger_signal_id" not in finding.trends
    assert finding.provenance["trigger_signal_id"] == "s" * 32


# ---- facts, trends, and the gap between them -------------------------------


def test_a_key_that_holds_still_describes_the_problem():
    facts, trends, partial = bundle.split_payloads(
        [{"reason": "OOMKilled", "restart_count": 5}, {"reason": "OOMKilled", "restart_count": 20}]
    )

    assert facts == {"reason": "OOMKilled"}
    assert trends == {"restart_count": [5, 20]}
    assert partial == []


def test_a_key_missing_from_some_occurrences_is_a_gap_not_a_trend():
    """The regression this test exists for rendered as `waiting_reason : X → X`.

    Judging presence and value together made an absent key look like a value changing
    into itself, which reads as a broken diff rather than as a field the collector did
    not always set. The gap is real and worth stating; it is just not movement."""
    facts, trends, partial = bundle.split_payloads(
        [{"reason": "Error"}, {"reason": "Error", "waiting_reason": "CrashLoopBackOff"}]
    )

    assert facts["waiting_reason"] == "CrashLoopBackOff"
    assert "waiting_reason" not in trends
    assert "waiting_reason" in partial


def test_a_key_that_is_both_intermittent_and_moving_is_still_a_trend():
    facts, trends, partial = bundle.split_payloads(
        [{"count": 1}, {"other": True}, {"count": 9}]
    )

    assert trends["count"] == [1, 9]
    assert "count" in partial


def test_trend_endpoints_come_from_occurrences_that_had_the_key(buffer):
    """[first, last] over present values, never over the raw occurrence list — an
    endpoint of None would say the counter started at nothing.

    The gaps are at the ends on purpose. With them in the middle the two
    implementations agree on every input, and the test passes against a version that
    indexes the occurrence list directly: a case that cannot distinguish the rule it
    is named after is decoration, not coverage."""
    payloads = [{}, {"count": 10}, {"count": 40}, {}]
    store([
        make_signal(when=NOW - timedelta(minutes=m), payload=p)
        for m, p in zip((12, 9, 6, 3), payloads, strict=True)
    ])

    finding = build().findings[0]

    assert finding.trends["count"] == [10, 40]
    assert None not in finding.trends["count"]


# ---- collapse --------------------------------------------------------------


def test_repeated_occurrences_collapse_to_one_finding(buffer):
    """Eleven rows saying OOMKilled are one fact seen eleven times. The buffer keeps
    them apart because spacing is only recoverable while they are separate; the
    assembler is where they come back together."""
    times = [NOW - timedelta(minutes=m) for m in (20, 15, 10, 5)]
    store([make_signal(when=t, payload={"reason": "OOMKilled"}) for t in times])

    findings = build().findings

    assert len(findings) == 1
    assert findings[0].occurrences == 4
    assert findings[0].first_seen == times[0]
    assert findings[0].last_seen == times[-1]


def test_distinct_problems_stay_distinct(buffer):
    store([
        make_signal(when=NOW - timedelta(minutes=8), key="app|OOMKilled|137"),
        make_signal(when=NOW - timedelta(minutes=4), key="app|Unhealthy|0"),
    ])

    assert len(build().findings) == 2


# ---- the message pair ------------------------------------------------------


def test_the_template_is_kept_and_the_raw_message_becomes_one_exemplar(buffer):
    store([
        make_signal(
            when=NOW - timedelta(minutes=m),
            payload={"message_template": "back-off <dur> restarting", "message": msg},
        )
        for m, msg in ((7, "back-off 20s restarting"), (3, "back-off 5m0s restarting"))
    ])

    finding = build().findings[0]

    assert finding.facts["message_template"] == "back-off <dur> restarting"
    assert "message" not in finding.facts
    assert "message" not in finding.trends
    assert finding.sample == "back-off 5m0s restarting"


def test_a_message_with_no_template_is_left_alone(buffer):
    """Folding only ever removes a duplicate. Without a template the message is the
    only statement of the problem, and dropping it would lose the finding's content."""
    store([make_signal(when=NOW - timedelta(minutes=5), payload={"message": "no such host"})])

    finding = build().findings[0]

    assert finding.facts["message"] == "no such host"
    assert finding.sample is None


# ---- correlation -----------------------------------------------------------


def test_a_log_excerpt_states_which_failure_it_explains(buffer):
    crash = make_signal(when=NOW - timedelta(minutes=6), payload={"reason": "OOMKilled"})
    store([crash])

    excerpt = make_signal(
        when=NOW - timedelta(minutes=6),
        source=SignalSource.K8S_LOGS,
        kind=SignalKind.LOG_EXCERPT,
        key="app|previous",
        payload=partition_payload(
            {"log_status": "empty", "trigger_fingerprint": crash.fingerprint},
            frozenset({"trigger_fingerprint"}),
        ),
    )
    store([excerpt], source=SignalSource.K8S_LOGS)

    linked = next(f for f in build().findings if f.source == SignalSource.K8S_LOGS)

    assert linked.explains == crash.fingerprint


def test_a_trigger_outside_the_bundle_is_kept_rather_than_cleared(buffer):
    """The trigger existed — the collector saw it — and it fell outside this window or
    this subject. That is a fact about the bundle's edges, not about the cluster, and
    silently dropping the pointer would state the excerpt had no cause."""
    excerpt = make_signal(
        when=NOW - timedelta(minutes=5),
        source=SignalSource.K8S_LOGS,
        kind=SignalKind.LOG_EXCERPT,
        payload=partition_payload(
            {"trigger_fingerprint": "f" * 32}, frozenset({"trigger_fingerprint"})
        ),
    )
    store([excerpt], source=SignalSource.K8S_LOGS)

    assert build().findings[0].explains == "f" * 32


# ---- ranking ---------------------------------------------------------------


def test_findings_are_ranked_by_severity_then_recency(buffer):
    """Severity is a StrEnum, so an accidental sort would order these alphabetically
    and put info above warning."""
    store([
        make_signal(when=NOW - timedelta(minutes=2), key="a", severity=Severity.INFO),
        make_signal(when=NOW - timedelta(minutes=9), key="b", severity=Severity.ERROR),
        make_signal(when=NOW - timedelta(minutes=5), key="c", severity=Severity.WARNING),
    ])

    assert [f.severity for f in build().findings] == [
        Severity.ERROR,
        Severity.WARNING,
        Severity.INFO,
    ]


# ---- what the sources managed ----------------------------------------------


def test_every_expected_source_is_reported_even_with_nothing_to_say(buffer):
    store([make_signal(when=NOW - timedelta(minutes=5))])

    reported = {s.source for s in build().sources}

    assert reported == set(bundle.EXPECTED_SOURCES)


def test_a_source_that_never_ran_is_distinct_from_one_that_failed(buffer):
    """Three-state status describes an attempt. A source with no run row at all made no
    attempt, which the three states cannot express — and left out of the bundle it
    reads as a source that simply had no findings."""
    store([make_signal(when=NOW - timedelta(minutes=5))])

    silent = next(s for s in build().sources if s.source == str(SignalSource.K8S_EVENTS))
    ran = next(s for s in build().sources if s.source == str(SignalSource.K8S_PODS))

    assert silent.never_ran is True
    assert silent.last_started is None
    assert ran.never_ran is False
    assert ran.status == SourceStatus.OK


# ---- the window ------------------------------------------------------------


def test_evidence_outside_the_window_is_not_in_the_bundle(buffer):
    store([
        make_signal(when=NOW - timedelta(minutes=5)),
        make_signal(when=NOW - timedelta(hours=4), key="old"),
    ])

    assert len(build().findings) == 1


def test_an_empty_window_still_reports_its_sources(buffer):
    """The bundle a healthy cluster produces. It has to be distinguishable from the one
    produced when nothing was collected at all, which is what the source block is for."""
    result = build()

    assert result.findings == []
    assert all(s.never_ran for s in result.sources)


@pytest.mark.parametrize("subject", [POD, None])
def test_the_subject_filter_is_optional(buffer, subject):
    store([
        make_signal(when=NOW - timedelta(minutes=5)),
        make_signal(when=NOW - timedelta(minutes=4), pod="other-pod", key="z"),
    ])

    assert len(build(subject).findings) == (1 if subject else 2)


# ---- ranking by what a finding says ----------------------------------------


def test_a_log_that_saw_nothing_cannot_outrank_the_failure_it_explains(buffer):
    """Severity is inherited from the trigger because a log's own text cannot be
    ranked — reading a level out of arbitrary formatting would make an unfamiliar
    format look calmer than a familiar one. That was right at collection time and is
    wrong at ranking time: an empty read carries the crash's severity and none of its
    evidence, so left alone it sorts above the crash."""
    store([
        make_signal(when=NOW - timedelta(minutes=5), key="crash", severity=Severity.ERROR),
    ])
    store(
        [
            make_signal(
                when=NOW - timedelta(minutes=4),
                key="app|previous",
                source=SignalSource.K8S_LOGS,
                kind=SignalKind.LOG_EXCERPT,
                severity=Severity.ERROR,
                payload={"log_status": str(SourceStatus.EMPTY)},
            )
        ],
        source=SignalSource.K8S_LOGS,
    )

    ordered = build().findings

    assert ordered[0].source == SignalSource.K8S_PODS
    assert ordered[1].observational is True
    assert ordered[1].severity == Severity.ERROR
    assert ordered[1].rank < ordered[0].rank


def test_being_unable_to_see_ranks_above_having_seen_nothing(buffer):
    """Different facts. Empty means the container printed nothing; unavailable means
    this agent is blind, which is worth more of a reader's attention even though
    neither carries evidence about the workload."""
    for key, status in (("a", SourceStatus.EMPTY), ("b", SourceStatus.UNAVAILABLE)):
        store(
            [
                make_signal(
                    when=NOW - timedelta(minutes=5),
                    key=key,
                    source=SignalSource.K8S_LOGS,
                    kind=SignalKind.LOG_EXCERPT,
                    severity=Severity.ERROR,
                    payload={"log_status": str(status)},
                )
            ],
            source=SignalSource.K8S_LOGS,
        )

    blind, quiet = build().findings

    assert blind.blind is True
    assert quiet.blind is False
    assert blind.rank > quiet.rank


def test_a_finding_that_saw_something_keeps_its_severity(buffer):
    store(
        [
            make_signal(
                when=NOW - timedelta(minutes=5),
                source=SignalSource.K8S_LOGS,
                kind=SignalKind.LOG_EXCERPT,
                severity=Severity.ERROR,
                payload={"log_status": str(SourceStatus.OK), "excerpt": "panic"},
            )
        ],
        source=SignalSource.K8S_LOGS,
    )

    finding = build().findings[0]

    assert finding.observational is False
    assert finding.rank == bundle.SEVERITY_RANK[str(Severity.ERROR)]


# ---- counters --------------------------------------------------------------


def test_a_counter_is_diffed_across_its_observations(buffer):
    store([
        make_signal(when=NOW - timedelta(minutes=m), payload={"restart_count": n})
        for m, n in ((9, 110), (6, 118), (3, 124))
    ])

    delta = build().findings[0].deltas[0]

    assert (delta.key, delta.first, delta.last, delta.change) == ("restart_count", 110, 124, 14)


def test_a_restarted_counter_is_summed_rather_than_subtracted_across(buffer):
    """Aggregation is client-side and cached in the reporting component's memory, so a
    kubelet restart abandons one object and starts another from one. Both stay live and
    both land on this fingerprint. Subtracting straight across gives 6 - 38, a decrease
    that never happened; the series that appeared later began inside the window, so its
    whole value is what it contributed."""
    store([
        make_signal(when=NOW - timedelta(minutes=m), payload=marked({"count": n, "event_uid": u}, "event_uid"))
        for m, n, u in ((9, 30, "uid-old"), (6, 38, "uid-old"), (3, 6, "uid-new"))
    ])

    delta = build().findings[0].deltas[0]

    assert delta.series == 2
    assert delta.change == (38 - 30) + 6
    assert "summed" in delta.basis


def test_a_counter_that_predates_the_provenance_change_is_still_one_series(buffer):
    """event_uid moved into _provenance, and a window spanning that change holds rows
    of both shapes. Read only in its new home, the older rows have no series identity,
    one uninterrupted counter looks like two, and the later half is summed as though it
    had started from zero — a reset invented by the migration, in the field that exists
    to make real resets visible."""
    flat = {"count": 30, "event_uid": "uid-1"}
    nested = marked({"count": 93, "event_uid": "uid-1"}, "event_uid")
    store([
        make_signal(when=NOW - timedelta(minutes=9), payload=flat),
        make_signal(when=NOW - timedelta(minutes=3), payload=nested),
    ])

    delta = build().findings[0].deltas[0]

    assert delta.series == 1
    assert delta.change == 63


def test_a_single_observation_of_a_series_that_began_in_the_window_is_its_own_delta(buffer):
    """The resolution of the one-occurrence problem, and it comes from the field that
    looked like pure provenance until the live output was read: api_first_timestamp.
    With one row there is nothing to subtract from, but a series that started inside
    the window has an absolute value that already is the change."""
    began = iso(NOW - timedelta(minutes=10))
    store([
        make_signal(
            when=NOW - timedelta(minutes=5),
            payload={"count": 67, "api_first_timestamp": began},
        )
    ])

    delta = build().findings[0].deltas[0]

    assert delta.change == 67
    assert "began inside the window" in delta.basis


def test_a_single_observation_of_an_older_series_states_no_change(buffer):
    """The honest answer is the absolute value and no subtraction. A counter is only
    meaningful relative to the lifetime of the thing counting it, and here that
    lifetime starts before anything this bundle can see."""
    began = iso(START - timedelta(hours=2))
    store([
        make_signal(
            when=NOW - timedelta(minutes=5),
            payload={"count": 67, "api_first_timestamp": began},
        )
    ])

    finding = build().findings[0]

    assert finding.deltas == []
    assert finding.facts["count"] == 67


def test_a_number_that_goes_down_is_not_a_counter(buffer):
    """container_lifetime_seconds is the live example: 59 then 51 is two containers
    that died after different intervals. A difference across that describes nothing
    that happened, so it stays a trend."""
    store([
        make_signal(when=NOW - timedelta(minutes=m), payload={"container_lifetime_seconds": n})
        for m, n in ((9, 59), (3, 51))
    ])

    finding = build().findings[0]

    assert finding.deltas == []
    assert finding.trends["container_lifetime_seconds"] == [59, 51]


# ---- the window ------------------------------------------------------------


def test_the_window_reaches_back_to_where_the_leading_problem_started(buffer):
    """A window chosen before the evidence is read describes the alert, not the
    incident. A crash three hours old inside a one-hour window looks an hour old, and
    that invites a correlation with whatever else happened an hour ago."""
    began = NOW - timedelta(seconds=config.BUNDLE_LOOKBACK_SECONDS + 900)
    store([
        make_signal(when=began, payload={"reason": "OOMKilled"}),
        make_signal(when=NOW - timedelta(minutes=2), payload={"reason": "OOMKilled"}),
    ])

    with lz.connect() as conn:
        start, end, basis = bundle.derive_window(conn, CLUSTER, NOW, POD)

    assert start == began
    assert basis == bundle.WINDOW_EXTENDED


def test_the_window_stops_at_the_retention_horizon(buffer):
    """The cap is not a performance bound. The buffer holds two days, so a window
    reaching past it becomes a window over whatever survived the sweep — and reports
    the sweep's edge as the moment the problem began."""
    ancient = NOW - timedelta(seconds=config.BUNDLE_MAX_LOOKBACK_SECONDS + 3600)
    store([
        make_signal(when=ancient, payload={"reason": "OOMKilled"}),
        make_signal(when=NOW - timedelta(minutes=2), payload={"reason": "OOMKilled"}),
    ])

    with lz.connect() as conn:
        start, _, basis = bundle.derive_window(conn, CLUSTER, NOW, POD)

    assert basis == bundle.WINDOW_CAPPED
    assert start > ancient


def test_a_problem_that_started_inside_the_window_does_not_move_it(buffer):
    store([make_signal(when=NOW - timedelta(minutes=5), payload={"reason": "OOMKilled"})])

    with lz.connect() as conn:
        _, _, basis = bundle.derive_window(conn, CLUSTER, NOW, POD)

    assert basis == bundle.WINDOW_FIXED


# ---- the budget ------------------------------------------------------------


def test_what_the_budget_leaves_out_is_recorded(buffer):
    """The same rule as declined targets, buffer drops and a truncated excerpt. A
    bundle that omits silently reads as complete, and that is the one failure a
    reasoner cannot detect from the inside."""
    store([
        make_signal(when=NOW - timedelta(minutes=n), key=f"k{n}", severity=Severity.WARNING)
        for n in range(2, 8)
    ])

    result = build(max_findings=2)

    assert len(result.findings) == 2
    assert len(result.omitted) == 4
    assert all(o.occurrences == 1 for o in result.omitted)
    assert all("rank" in o.reason for o in result.omitted)


def test_the_budget_keeps_the_highest_ranked(buffer):
    store([
        make_signal(when=NOW - timedelta(minutes=5), key="low", severity=Severity.INFO),
        make_signal(when=NOW - timedelta(minutes=5), key="high", severity=Severity.CRITICAL),
    ])

    result = build(max_findings=1)

    assert result.findings[0].severity == Severity.CRITICAL
    assert result.omitted[0].severity == str(Severity.INFO)


# ---- degradation -----------------------------------------------------------


def test_an_unreachable_store_costs_the_history_claims_and_says_which(buffer, monkeypatch):
    monkeypatch.setattr(bundle, "history_for", lambda fps, since: ({}, [bundle.HISTORY_DROPPED]))
    store([make_signal(when=NOW - timedelta(minutes=5))])

    result = build(history=True)

    assert result.degraded == [bundle.HISTORY_DROPPED]
    assert result.findings[0].history is None


def test_history_reaches_the_finding_when_the_store_answers(buffer, monkeypatch):
    """The claim the store exists to make. first_seen_ever lives there rather than in
    the buffer because the buffer holds two days: asked here it would answer "never
    seen before" for anything older, which is the most confident way to be wrong."""
    seen = bundle.History(first_seen_ever=NOW - timedelta(days=9), is_new=False)
    monkeypatch.setattr(
        bundle, "history_for", lambda fingerprints, since: ({fp: seen for fp in fingerprints}, [])
    )
    store([make_signal(when=NOW - timedelta(minutes=5))])

    result = build(history=True)

    assert result.degraded == []
    assert result.findings[0].history is not None
    assert result.findings[0].history.is_new is False


def test_a_window_spanning_a_normalizer_change_says_so(buffer):
    """A recurrence query across a bump is answering about the keying rules as much as
    about the cluster: the same problem acquires a new fingerprint the moment the rules
    change, and first_seen then reports 'never' with nothing raising anywhere."""
    store([make_signal(when=NOW - timedelta(minutes=m)) for m in (9, 3)])

    with lz.connect() as conn:
        rows = conn.execute("SELECT signal_id FROM signals ORDER BY event_time").fetchall()
        conn.execute(
            "UPDATE signals SET normalizer_version = 'v2' WHERE signal_id = ?",
            (rows[-1]["signal_id"],),
        )

    result = build()

    assert len(result.normalizer_versions) == 2
    assert any("normalizer" in note for note in result.degraded)


# ---- the two receipts ------------------------------------------------------


def test_the_evidence_receipt_ignores_how_the_findings_are_ordered(buffer):
    """Order is what the model reads, so it belongs to the prompt receipt. Changing a
    ranking rule changes the prompt; it does not change which evidence was selected."""
    store([
        make_signal(when=NOW - timedelta(minutes=5), key="a", severity=Severity.ERROR),
        make_signal(when=NOW - timedelta(minutes=4), key="b", severity=Severity.INFO),
    ])

    result = build()
    before = bundle.evidence_receipt(result)
    result.findings.reverse()

    assert bundle.evidence_receipt(result) == before


def test_the_evidence_receipt_moves_when_a_source_goes_dark(buffer):
    """A diagnosis made blind to a source and one made with it are different verdicts
    against different inputs, and the corpus has to be able to tell them apart."""
    store([make_signal(when=NOW - timedelta(minutes=5))])

    result = build()
    before = bundle.evidence_receipt(result)
    result.sources[0].status = SourceStatus.UNAVAILABLE

    assert bundle.evidence_receipt(result) != before


def test_the_evidence_receipt_ignores_provenance_and_observation_timing(buffer):
    """Fold these in and no two runs ever share a receipt, which is exactly as useless
    as a hash that never moves."""
    store([make_signal(when=NOW - timedelta(minutes=5))])

    result = build()
    before = bundle.evidence_receipt(result)

    result.findings[0].provenance = {"trigger_signal_id": "z" * 32}
    result.findings[0].signal_ids = ["nonsense"]
    result.sources[0].last_started = NOW
    result.sources[0].signals = 999

    assert bundle.evidence_receipt(result) == before


def test_the_evidence_receipt_records_what_was_left_out(buffer):
    """Forty findings dropped and none dropped are different inputs."""
    store([
        make_signal(when=NOW - timedelta(minutes=n), key=f"k{n}", severity=Severity.WARNING)
        for n in range(2, 6)
    ])

    assert bundle.evidence_receipt(build(max_findings=4)) != bundle.evidence_receipt(
        build(max_findings=2)
    )


def test_the_prompt_receipt_moves_with_the_wording(buffer):
    store([make_signal(when=NOW - timedelta(minutes=5))])

    rendered = bundle.render(build())

    assert bundle.prompt_receipt(rendered) != bundle.prompt_receipt(rendered + " ")
    assert bundle.prompt_receipt(rendered) == bundle.prompt_receipt(rendered)
