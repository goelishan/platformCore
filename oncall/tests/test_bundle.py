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

from oncall import landing_zone as lz
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
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


def build(subject: str | None = POD) -> bundle.Bundle:
    with lz.connect() as conn:
        return bundle.build(conn, CLUSTER, START, END, subject_name=subject)


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
