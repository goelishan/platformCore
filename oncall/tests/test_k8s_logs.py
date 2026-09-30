"""
The log collector.

Everything except collect() is pure, so the target selection, the stream choice and
the excerpt are all exercisable with no cluster. What needs a buffer gets one, because
this is the first collector whose status depends on what another collector wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kubernetes.client.exceptions import ApiException

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
    """A Fetch the real read path could have produced.

    raw is derived from text unless a caller states it, because in _read_log text is
    the decode of raw and the two can never disagree about being empty. A fixture that
    carried an excerpt with no bytes described a value the collector cannot build, and
    blob writing keys off raw."""
    base = dict(
        status=str(SourceStatus.OK),
        text=STREAM,
        stream=k8s_logs.STREAM_PREVIOUS,
        truncated=False,
        fell_back=False,
        error=None,
    )
    merged = {**base, **overrides}
    merged.setdefault("raw", str(merged["text"]).encode())
    return k8s_logs.Fetch(**merged)


def test_event_time_is_inherited_from_the_trigger():
    """The excerpt belongs to the moment the failure happened. Taking the clock instead
    would collapse event_time into collected_at and mint a new row every cycle."""
    signal = k8s_logs.build_signal(
        _target(), _fetch(), k8s_logs.build_excerpt(STREAM), None, "oncall-dev", NOW, NOW
    )
    assert signal.event_time.isoformat().startswith("2026-08-14T03:14:07")


def test_severity_describes_the_fetch_not_the_trigger():
    """It used to be inherited, and a live run showed that was unstable: an excerpt
    keeps the severity of whichever trigger was inside the lookback window, and a pod
    stuck waiting has a frozen event_time that ages out of it, leaving only events able
    to trigger. The same fingerprint changed severity between runs because the schedule
    moved and nothing about the pod did."""
    signal = k8s_logs.build_signal(
        _target(severity=str(Severity.CRITICAL)),
        _fetch(),
        k8s_logs.build_excerpt(STREAM),
        None,
        "oncall-dev",
        NOW,
        NOW,
    )

    assert signal.severity == Severity.INFO
    # The incident's severity is still reachable, on the signal this is attached to.
    assert provenance_of(signal.payload)["trigger_fingerprint"]


def test_an_unreadable_log_is_a_warning_because_it_is_a_gap_in_evidence():
    signal = k8s_logs.build_signal(
        _target(severity=str(Severity.INFO)),
        _fetch(status=str(SourceStatus.UNAVAILABLE), text="", error="ApiException 500"),
        None,
        None,
        "oncall-dev",
        NOW,
        NOW,
    )

    assert signal.severity == Severity.WARNING


def test_severity_no_longer_moves_with_the_trigger():
    """Two triggers of different severity over one identical fetch. Before this rule
    those produced two severities for one fingerprint."""
    from_pods = k8s_logs.build_signal(
        _target(severity=str(Severity.ERROR)), _fetch(),
        k8s_logs.build_excerpt(STREAM), None, "oncall-dev", NOW, NOW,
    )
    from_events = k8s_logs.build_signal(
        _target(severity=str(Severity.WARNING)), _fetch(),
        k8s_logs.build_excerpt(STREAM), None, "oncall-dev", NOW, NOW,
    )

    assert from_pods.fingerprint == from_events.fingerprint
    assert from_pods.severity == from_events.severity


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


# ---- the read ---------------------------------------------------------------
# The only part of this module that crosses the client boundary, and the part that was
# wrong in production for two weeks with a green suite. These stubs answer with bytes
# because that is what the transport carries; the previous stub answered with a str
# because that is what the generated signature claims, and it was the signature that
# was wrong. This is closer to the wire and still not a substitute for a test against a
# real API server — it cannot catch the next thing the client does differently from its
# own declaration.


class _Resp:
    """What the client returns under _preload_content=False: the response, unread."""

    def __init__(self, data: bytes):
        self.data = data


class _Client:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls: list[bool] = []

    def read_namespaced_pod_log(self, **kwargs):
        self.calls.append(kwargs["previous"])
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture()
def client(monkeypatch):
    def install(*answers) -> _Client:
        stub = _Client(*answers)
        monkeypatch.setattr(k8s_logs.k8s, "core_v1", lambda: stub)
        return stub

    return install


def test_the_body_is_decoded_rather_than_stringified(client):
    """The bug that made this collector's output worthless for two weeks.

    The generated client produced its declared str by calling str() on the bytes, so
    the caller received the repr of a bytes object: one line, no parseable timestamps,
    and b'' read as truthy content. Asserting on the decoded text is the cheapest
    statement that the bytes were owned here."""
    client(_Resp(STREAM.encode()))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.text == STREAM
    assert fetch.raw == STREAM.encode()
    assert "\\n" not in fetch.text
    assert len(fetch.text.splitlines()) > 1


def test_an_empty_log_is_empty_and_not_ok(client):
    """b'' is four truthy characters once it has been stringified, which is how a
    container that printed nothing was recorded as successfully read."""
    client(_Resp(b""))

    assert k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT).status == str(
        SourceStatus.EMPTY
    )


def test_truncation_is_measured_on_the_bytes_the_server_sent(client, monkeypatch):
    """limit_bytes bounds bytes, so the cap has to be compared against bytes.

    One prefixed line carrying eight undecodable bytes: 29 bytes on the wire under a
    cap of forty, so nothing was cut. Each undecodable byte decodes to U+FFFD, which
    re-encodes to three bytes, so measuring the decoded form finds forty-five and
    claims a truncation that never happened. A replacement character counted as
    though it were the bytes it stands in for.

    The cap and the payload are chosen so the two measurements disagree. An earlier
    version of this test used a cap of eight, where every candidate measurement
    answers True and the case cannot tell them apart. The runtime prefix is present
    because timestamps=True guarantees it, and a body without one is not a log."""
    monkeypatch.setattr(config, "LOG_LIMIT_BYTES", 40)
    body = b"2026-09-29T12:00:00Z " + b"\xff" * 8
    client(_Resp(body))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == str(SourceStatus.OK)
    assert len(fetch.raw) == 29
    assert fetch.truncated is False


def test_a_missing_previous_container_falls_back_and_says_so(client):
    """A 400 here is a fact about this pod, not a failure to see it: the container has
    not restarted yet. The running stream is real evidence and the row records that it
    is not the stream that was asked for."""
    stub = client(ApiException(status=400, reason="Bad Request"), _Resp(STREAM.encode()))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_PREVIOUS)

    assert stub.calls == [True, False]
    assert fetch.stream == k8s_logs.STREAM_CURRENT
    assert fetch.fell_back is True


def test_one_unreadable_target_does_not_take_the_source_down(client):
    """Recorded against this target alone. Thirty-nine other pods are real evidence and
    withholding them helps nobody, so the failure stays local and named."""
    client(ApiException(status=500, reason="Internal Server Error"))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == str(SourceStatus.UNAVAILABLE)
    assert fetch.raw == b""
    assert "500" in (fetch.error or "")


# ---- nothing to read from --------------------------------------------------
# Five of nine rows in the first live corpus contained nothing but "ApiException 400:
# Bad Request". badimage never pulled an image and configerror never resolved its
# ConfigMap, so neither container has ever existed and every fetch was doomed before it
# was made. Recorded as unavailable — "nothing is known" — when in fact everything was
# known: there is no container, therefore there is no log.


def test_a_container_that_never_started_is_not_fetched_at_all():
    """Knowable from the trigger without an API call, which is the difference between
    two doomed requests per pod per cycle and none."""
    for reason in ("ErrImagePull", "ImagePullBackOff", "CreateContainerConfigError"):
        payload = {"reason": reason, "restart_count": 0, "exit_code": None,
                   "waiting_reason": reason, "container": "app"}
        assert k8s_logs._streams_for(payload, str(SignalSource.K8S_PODS)) == (
            k8s_logs.STREAM_NONE,
        )


def test_a_restarted_container_now_failing_to_pull_is_still_read():
    """The reason alone is not enough. A pod that ran eleven times and is now in
    ImagePullBackOff because someone deleted the tag does have a previous stream, and
    refusing to read it would discard the only evidence there is."""
    payload = {"reason": "ImagePullBackOff", "restart_count": 11, "exit_code": 1,
               "waiting_reason": "ImagePullBackOff", "container": "app"}

    assert k8s_logs._streams_for(payload, str(SignalSource.K8S_PODS)) != (
        k8s_logs.STREAM_NONE,
    )


def test_the_no_container_marker_short_circuits_the_read(client):
    stub = client()
    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_NONE)

    assert fetch.status == k8s_logs.LOG_STATUS_NO_CONTAINER
    assert stub.calls == []          # no request was made at all
    assert fetch.error is None       # and none is claimed


def test_a_400_on_the_running_stream_means_no_container_not_unavailable(client):
    """The gap the trigger-side rule cannot close, because an event payload carries no
    restart count. The API says the same thing with its own authority."""
    client(ApiException(status=400, reason="Bad Request"))
    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == k8s_logs.LOG_STATUS_NO_CONTAINER
    assert fetch.error is None


def test_a_500_is_still_unavailable(client):
    """The distinction has to stay narrow. A server error genuinely means nothing is
    known, and collapsing it into no_container would claim a pod has no container
    because the API server was busy."""
    client(ApiException(status=500, reason="Internal Server Error"))
    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == str(SourceStatus.UNAVAILABLE)
    assert "500" in fetch.error


# ---- the runtime's complaint is not the container's output -----------------


def test_a_runtime_error_body_is_not_stored_as_a_log(client):
    """kubelet answers 200 with an error string rather than a status code when the
    runtime cannot hand over a log. Stored naively that arrived as log_status=ok with
    one line of content, and the same fingerprint then held one row of infrastructure
    noise and one row of the real failure with nothing to tell them apart."""
    body = b"unable to retrieve container logs for containerd://3bdd38793aeeb04f8d55"
    client(_Resp(body))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == str(SourceStatus.UNAVAILABLE)
    assert fetch.text == ""
    assert fetch.error is not None
    assert "unable to retrieve" in fetch.error

def test_a_log_line_that_merely_mentions_the_phrase_is_still_a_log(client):
    """An application logging "unable to retrieve container logs from upstream" is
    reporting its own problem. The runtime prefixed the line, so it came from the
    stream, and discarding it would delete the diagnosis."""
    client(_Resp(b"2026-09-07T18:19:20Z ERROR unable to retrieve container logs from upstream\n"))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == str(SourceStatus.OK)
    assert "upstream" in fetch.text

def test_an_unfamiliar_runtime_complaint_is_still_not_a_log(client):
    """The reason the check is structural. A wording no list anticipated, with no
    runtime timestamp on any line, did not come from the log stream. A curated
    pattern stores this as log_status=ok with content."""
    client(_Resp(b"failed to open log file \"/var/log/pods/x/0.log\": no such file or directory"))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == str(SourceStatus.UNAVAILABLE)
    assert fetch.text == ""
    assert fetch.error is not None
    assert "failed to open log file" in fetch.error

def test_unprefixed_continuation_lines_do_not_disqualify_a_real_stream(client):
    """A stack trace: one prefixed line, then continuation lines with none of their
    own. Requiring every line to carry the prefix would throw away the fetch most
    worth having."""
    client(_Resp(
        b"2026-09-29T12:00:00Z panic: runtime error: nil map\n"
        b"\tgoroutine 1 [running]:\n"
        b"\tmain.main()\n"
    ))

    fetch = k8s_logs._read_log(_target(), k8s_logs.STREAM_CURRENT)

    assert fetch.status == str(SourceStatus.OK)
    assert "goroutine" in fetch.text
