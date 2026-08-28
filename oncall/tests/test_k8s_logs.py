"""
The log collector.

Everything except collect() is pure, so the target selection, the stream choice and
the excerpt are all exercisable with no cluster. What needs a buffer gets one, because
this is the first collector whose status depends on what another collector wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oncall import config
from oncall import landing_zone as lz
from oncall.collectors import k8s_logs
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    provenance_of,
)

NOW = datetime(2026, 8, 14, 3, 14, 7, tzinfo=UTC)


def _trigger(
    source=SignalSource.K8S_PODS,
    kind=SignalKind.POD_STATE,
    severity=Severity.ERROR,
    pod="web-1",
    payload=None,
    event_time=NOW,
) -> Signal:
    return Signal(
        source=source,
        kind=kind,
        cluster="oncall-dev",
        event_time=event_time,
        namespace="oncall-lab",
        subject=Subject(kind="Pod", name=pod, uid=f"u-{pod}"),
        node="node-a",
        severity=severity,
        dedupe_key=f"app|CrashLoopBackOff|1|{pod}",
        payload=payload or {"container": "app", "waiting_reason": "CrashLoopBackOff"},
    )


def _row(signal: Signal, run_id=None):
    with lz.connect() as conn:
        lz.write_signals(conn, run_id, [signal])
        return conn.execute(
            "SELECT * FROM signals WHERE signal_id = ?", (signal.signal_id,)
        ).fetchone()


# ---- choosing the stream ---------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # In backoff: the current container is empty or has not failed yet.
        ({"waiting_reason": "CrashLoopBackOff"}, (k8s_logs.STREAM_PREVIOUS,)),
        # Already terminated: the evidence is in the container that exited.
        ({"exit_code": 137}, (k8s_logs.STREAM_PREVIOUS,)),
        # Running now, killed earlier. Every live indicator reads healthy, so the
        # previous container's log is the only evidence anything happened — and the
        # current one is still worth having for what came after.
        (
            {"restart_count": 3, "reason": "Running"},
            (k8s_logs.STREAM_PREVIOUS, k8s_logs.STREAM_CURRENT),
        ),
        ({"reason": "Running"}, (k8s_logs.STREAM_CURRENT,)),
    ],
)
def test_stream_follows_container_state(payload, expected):
    assert k8s_logs._streams_for(payload, str(SignalSource.K8S_PODS)) == expected


def test_an_event_trigger_asks_for_previous_first():
    """An event names a condition, not a container state, so there is nothing to read
    the answer off. Previous is the better guess at this severity, and a wrong guess is
    a 400 that falls back — cheap, unlike a wrong guess in a stored key."""
    assert k8s_logs._streams_for({}, str(SignalSource.K8S_EVENTS)) == (
        k8s_logs.STREAM_PREVIOUS,
    )


# ---- choosing the targets --------------------------------------------------


def test_one_target_per_pod_container_and_stream(buffer):
    rows = [
        _row(_trigger(pod="web-1", event_time=NOW)),
        _row(_trigger(pod="web-1", event_time=NOW - timedelta(minutes=2))),
    ]
    targets, declined = k8s_logs.targets_from_rows(rows, limit=10)

    assert len(targets) == 1
    assert declined == 0


def test_the_worst_trigger_wins_not_the_last(buffer):
    """The excerpt should be attached to the worst thing that happened in the window,
    not to whatever landed most recently."""
    rows = [
        _row(_trigger(pod="web-1", severity=Severity.WARNING, event_time=NOW)),
        _row(
            _trigger(
                pod="web-1",
                severity=Severity.CRITICAL,
                event_time=NOW - timedelta(minutes=5),
            )
        ),
    ]
    targets, _ = k8s_logs.targets_from_rows(rows, limit=10)

    assert targets[0].severity == str(Severity.CRITICAL)


def test_the_cap_reports_what_it_declined(buffer):
    """A run that quietly stopped at the cap is indistinguishable from a cluster with
    exactly that many broken pods, and during a cascading failure the difference is the
    entire diagnosis."""
    rows = [_row(_trigger(pod=f"web-{i}")) for i in range(5)]
    targets, declined = k8s_logs.targets_from_rows(rows, limit=2)

    assert len(targets) == 2
    assert declined == 3


def test_log_signals_never_trigger_more_log_collection(buffer):
    """Without the exclusion each cycle re-triggers on its own output forever — the
    same self-feeding loop the agent's own namespace is excluded to prevent."""
    _row(_trigger(source=SignalSource.K8S_LOGS, kind=SignalKind.LOG_EXCERPT))
    _row(_trigger(pod="web-2"))

    with lz.connect() as conn:
        rows = lz.log_targets(
            conn,
            "oncall-dev",
            NOW - timedelta(minutes=30),
            NOW + timedelta(minutes=30),
            tuple(config.LOG_TRIGGER_SEVERITIES),
        )

    assert {r["source"] for r in rows} == {str(SignalSource.K8S_PODS)}


def test_warning_is_a_trigger(buffer):
    """The restarted-but-currently-healthy container is a WARNING in k8s_pods, and it
    is the case where previous logs are the only evidence at all."""
    _row(_trigger(severity=Severity.WARNING))

    with lz.connect() as conn:
        rows = lz.log_targets(
            conn,
            "oncall-dev",
            NOW - timedelta(minutes=30),
            NOW + timedelta(minutes=30),
            tuple(config.LOG_TRIGGER_SEVERITIES),
        )

    assert len(rows) == 1


def test_info_signals_are_not_triggers(buffer):
    _row(_trigger(severity=Severity.INFO))

    with lz.connect() as conn:
        rows = lz.log_targets(
            conn,
            "oncall-dev",
            NOW - timedelta(minutes=30),
            NOW + timedelta(minutes=30),
            tuple(config.LOG_TRIGGER_SEVERITIES),
        )

    assert rows == []


# ---- the excerpt -----------------------------------------------------------

STREAM = (
    "2026-08-14T03:14:01.100000Z starting server on 10.244.0.7:8080\n"
    "2026-08-14T03:14:02.100000Z GET /healthz from 10.244.0.9\n"
    "2026-08-14T03:14:03.100000Z GET /healthz from 10.244.0.11\n"
    "2026-08-14T03:14:04.100000Z GET /healthz from 10.244.0.13\n"
    "2026-08-14T03:14:05.100000Z panic: runtime error: index out of range [3]\n"
    "goroutine 1 [running]:\n"
)


def test_repeated_lines_collapse_by_template():
    """Three health-check lines differing only by client IP are one line plus a count.
    The same templating that keys identity earns its second job here: without it four
    hundred identical lines eat the whole excerpt budget and push out the panic."""
    excerpt = k8s_logs.build_excerpt(STREAM)

    assert "(x3)" in excerpt.text
    assert excerpt.collapsed is True
    assert "panic: runtime error" in excerpt.text


def test_continuation_lines_are_kept():
    """A stack trace arrives as lines with no runtime timestamp of their own. Dropping
    unprefixed lines would discard the most useful part of the fetch."""
    assert "goroutine 1 [running]:" in k8s_logs.build_excerpt(STREAM).text


def test_covered_through_comes_from_the_runtime_not_the_application():
    """The rule against reading time out of log text is about the application's own
    formatting. This prefix is written by the container runtime because the fetch asked
    for timestamps, and it is the only honest answer to 'how far does this reach'."""
    assert k8s_logs.build_excerpt(STREAM).covered_through == "2026-08-14T03:14:05.100000Z"


def test_the_tail_is_kept_not_the_head():
    """The fetch streams oldest-first and the byte cap cuts the newest lines, so what
    survives has its most recent end nearest the failure."""
    excerpt = k8s_logs.build_excerpt(STREAM, max_lines=2)

    assert "panic: runtime error" in excerpt.text
    assert "starting server" not in excerpt.text


def test_secrets_are_removed_from_the_excerpt():
    stream = "2026-08-14T03:14:01.100000Z connecting with password=hunter2trombone\n"
    excerpt = k8s_logs.build_excerpt(stream)

    assert "hunter2trombone" not in excerpt.text
    assert excerpt.redacted is True


def test_the_byte_budget_is_enforced():
    stream = "".join(
        f"2026-08-14T03:14:{i:02d}.100000Z line number {i} of many\n" for i in range(60)
    )
    assert len(k8s_logs.build_excerpt(stream, max_bytes=200).text.encode()) <= 200


# ---- status ----------------------------------------------------------------


def test_no_upstream_run_reads_as_unavailable_not_empty():
    """No targets because the cluster is healthy and no targets because pod state never
    ran are the same input and opposite facts. Reporting empty for the second is the
    most confident possible statement of health made out of no information."""
    assert k8s_logs._upstream_verdict([]) is not None


def test_every_upstream_source_down_reads_as_unavailable():
    statuses = [
        {"source": "k8s_pods", "status": "unavailable"},
        {"source": "k8s_events", "status": "unavailable"},
    ]
    assert k8s_logs._upstream_verdict(statuses) is not None


def test_one_healthy_upstream_source_is_enough_to_look():
    """k8s_events being down is recorded in its own collection_runs row, which the
    assembler already reads. Re-reporting it here would say the same thing twice and
    suppress evidence that was collected successfully."""
    statuses = [
        {"source": "k8s_pods", "status": "ok"},
        {"source": "k8s_events", "status": "unavailable"},
    ]
    assert k8s_logs._upstream_verdict(statuses) is None


# ---- the emitted signal ----------------------------------------------------


def _target(**overrides) -> k8s_logs.Target:
    base = dict(
        namespace="oncall-lab",
        pod="web-1",
        uid="u-web-1",
        container="app",
        streams=(k8s_logs.STREAM_PREVIOUS,),
        node="node-a",
        owner_kind="Deployment",
        owner_name="web",
        severity=str(Severity.ERROR),
        event_time="2026-08-14T03:14:07.000000Z",
        trigger_source=str(SignalSource.K8S_PODS),
        trigger_fingerprint="f" * 32,
        trigger_signal_id="s" * 32,
    )
    return k8s_logs.Target(**{**base, **overrides})


def _fetch(**overrides) -> k8s_logs.Fetch:
    base = dict(
        status=str(SourceStatus.OK),
        text=STREAM,
        stream=k8s_logs.STREAM_PREVIOUS,
        truncated=False,
        fell_back=False,
        error=None,
    )
    return k8s_logs.Fetch(**{**base, **overrides})


def test_event_time_is_inherited_from_the_trigger():
    """The excerpt belongs to the moment the failure happened. Taking the clock instead
    would collapse event_time into collected_at and mint a new row every cycle."""
    signal = k8s_logs.build_signal(
        _target(), _fetch(), k8s_logs.build_excerpt(STREAM), None, "oncall-dev", NOW, NOW
    )
    assert signal.event_time.isoformat().startswith("2026-08-14T03:14:07")


def test_severity_is_inherited_never_parsed():
    signal = k8s_logs.build_signal(
        _target(severity=str(Severity.CRITICAL)),
        _fetch(),
        k8s_logs.build_excerpt(STREAM),
        None,
        "oncall-dev",
        NOW,
        NOW,
    )
    assert signal.severity == Severity.CRITICAL


def test_a_failed_read_is_distinguishable_from_an_empty_one():
    """A container that printed nothing and a container we could not read produce very
    different conclusions, and an absent excerpt cannot tell them apart."""
    unreadable = k8s_logs.build_signal(
        _target(),
        _fetch(status=str(SourceStatus.UNAVAILABLE), text="", error="ApiException 500"),
        None,
        None,
        "oncall-dev",
        NOW,
        NOW,
    )
    silent = k8s_logs.build_signal(
        _target(), _fetch(status=str(SourceStatus.EMPTY), text=""), None, None,
        "oncall-dev", NOW, NOW,
    )

    assert unreadable.payload["log_status"] == "unavailable"
    assert unreadable.payload["error"]
    assert silent.payload["log_status"] == "empty"
    assert "error" not in silent.payload


def test_a_truncated_fetch_says_where_it_stops():
    signal = k8s_logs.build_signal(
        _target(),
        _fetch(truncated=True),
        k8s_logs.build_excerpt(STREAM),
        None,
        "oncall-dev",
        NOW - timedelta(minutes=10),
        NOW,
    )

    assert signal.payload["truncated"] is True
    assert provenance_of(signal.payload)["window_start"]
    assert signal.payload["covered_through"] == "2026-08-14T03:14:05.100000Z"


def test_the_trigger_is_recorded_so_the_join_exists():
    signal = k8s_logs.build_signal(
        _target(), _fetch(), k8s_logs.build_excerpt(STREAM), "b" * 64, "oncall-dev",
        NOW, NOW,
    )

    assert provenance_of(signal.payload)["trigger_signal_id"] == "s" * 32
    assert signal.blob_id == "b" * 64


def test_re_running_the_same_trigger_is_idempotent():
    """event_time from the trigger plus a key of container|stream means a repeated
    cycle writes the same row rather than a new one per poll."""
    first = k8s_logs.build_signal(
        _target(), _fetch(), k8s_logs.build_excerpt(STREAM), None, "oncall-dev", NOW, NOW
    )
    again = k8s_logs.build_signal(
        _target(), _fetch(), k8s_logs.build_excerpt(STREAM), None, "oncall-dev", NOW, NOW
    )

    assert first.signal_id == again.signal_id
