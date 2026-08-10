"""
Landing zone contract tests.

Each test pins a decision that is cheap to reverse by accident and expensive to notice:
identity, incident_id survival across re-collection, recurrence grouping that keeps
spacing, retention exempting the eval corpus, and foreign keys actually being on.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope import (
    Owner,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    iso,
    parse,
)

CLUSTER = "platformcore"
NAMESPACE = "prod"
EVENT_TIME = datetime(2026, 8, 5, 3, 4, 12, tzinfo=UTC)

# container|reason|exit_code. The exit code is stable per failure mode — 1 is an
# application error, 137 is SIGKILL. A restart counter must never appear here: it
# increments, so every restart would land in its own recurrence group.
CRASH_KEY = "payments-api|CrashLoopBackOff|1"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Redirect the landing zone at a temp directory. config resolves paths at call
    time, so patching the module attributes is enough."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "oncall.db")
    monkeypatch.setattr(config, "BLOB_DIR", tmp_path / "blobs")
    lz.bootstrap()
    return tmp_path


def make_signal(
    name: str = "payments-api-7f9",
    event_time: datetime = EVENT_TIME,
    dedupe_key: str = CRASH_KEY,
    kind: SignalKind = SignalKind.POD_STATE,
    node: str | None = "ip-10-0-1-42",
) -> Signal:
    return Signal(
        source=SignalSource.K8S_PODS,
        kind=kind,
        cluster=CLUSTER,
        namespace=NAMESPACE,
        event_time=event_time,
        subject=Subject(kind="Pod", name=name),
        owner=Owner(kind="Deployment", name="payments-api"),
        node=node,
        dedupe_key=dedupe_key,
        payload={"restart_count": 1, "exit_code": 1},
    )


# ---- identity --------------------------------------------------------------


def test_iso_is_fixed_width_and_sorts_chronologically():
    zero_micros = iso(datetime(2026, 8, 5, 3, 4, 12, tzinfo=UTC))
    half_second = iso(datetime(2026, 8, 5, 3, 4, 12, 500000, tzinfo=UTC))

    assert len(zero_micros) == len(half_second) == 27
    assert zero_micros < half_second


def test_offset_timestamps_normalise_to_the_same_identity():
    from datetime import timezone

    ist = make_signal(event_time=EVENT_TIME.astimezone(timezone(timedelta(hours=5, minutes=30))))
    utc = make_signal(event_time=EVENT_TIME)

    assert ist.signal_id == utc.signal_id


def test_naive_datetime_is_rejected_at_the_door():
    with pytest.raises(ValidationError):
        make_signal(event_time=datetime(2026, 8, 5, 3, 4, 12))


def test_same_problem_different_time_shares_fingerprint_only():
    first = make_signal(event_time=EVENT_TIME)
    later = make_signal(event_time=EVENT_TIME + timedelta(minutes=5))

    assert first.fingerprint == later.fingerprint
    assert first.signal_id != later.signal_id


def test_different_problem_same_subject_splits_fingerprint():
    crash = make_signal(dedupe_key=CRASH_KEY)
    oom = make_signal(dedupe_key="payments-api|OOMKilled|137")

    assert crash.fingerprint != oom.fingerprint


def test_rescheduling_does_not_change_the_fingerprint():
    """node is context, not identity. The same failure on a different node is the
    same problem — putting node in the basis would shatter its recurrence group."""
    before = make_signal(node="ip-10-0-1-42")
    after = make_signal(node="ip-10-0-3-17")

    assert before.fingerprint == after.fingerprint
    assert before.signal_id == after.signal_id


def test_unknown_source_is_rejected():
    """source names are permanent and drive last_run(). A typo must fail loudly
    rather than create a phantom source no staleness check ever looks at."""
    with pytest.raises(ValidationError):
        Signal(
            source="k8s_pod",
            kind=SignalKind.POD_STATE,
            cluster=CLUSTER,
            event_time=EVENT_TIME,
        )


# ---- write path ------------------------------------------------------------


def test_recollection_is_idempotent(db):
    sig = make_signal()

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [sig, sig])
        lz.finish_run(conn, run, SourceStatus.OK, 1)

    with lz.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()

    assert rows["n"] == 1


def test_upsert_preserves_incident_id(db):
    """The trap. INSERT OR REPLACE is a DELETE + INSERT, so a routine overlapping poll
    would detach evidence from its incident and retention would then delete it."""
    sig = make_signal()

    with lz.connect() as conn:
        first_run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, first_run, [sig])
        incident = lz.open_incident(conn, CLUSTER, NAMESPACE, trigger="KubePodCrashLooping")
        attached = lz.attach_signals(
            conn,
            incident,
            CLUSTER,
            EVENT_TIME - timedelta(hours=1),
            EVENT_TIME + timedelta(minutes=1),
        )

    assert attached == 1

    with lz.connect() as conn:
        second_run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, second_run, [sig])

    with lz.connect() as conn:
        row = conn.execute("SELECT incident_id, run_id FROM signals").fetchone()

    assert row["incident_id"] == incident
    assert row["run_id"] == second_run


def test_attach_does_not_steal_from_an_earlier_incident(db):
    sig = make_signal()

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [sig])
        first = lz.open_incident(conn, CLUSTER, NAMESPACE, trigger="alert-a")
        lz.attach_signals(
            conn, first, CLUSTER, EVENT_TIME - timedelta(hours=1), EVENT_TIME + timedelta(hours=1)
        )
        second = lz.open_incident(conn, CLUSTER, NAMESPACE, trigger="alert-b")
        stolen = lz.attach_signals(
            conn, second, CLUSTER, EVENT_TIME - timedelta(hours=1), EVENT_TIME + timedelta(hours=1)
        )

    assert stolen == 0


def test_foreign_keys_are_enforced_per_connection(db):
    """Proves the pragma lives in the connection helper. In schema.sql it would only
    cover the bootstrap connection and every later one would run with FKs off."""
    with pytest.raises(sqlite3.IntegrityError):
        with lz.connect() as conn:
            conn.execute(
                "INSERT INTO diagnoses (diagnosis_id, incident_id, created_at) "
                "VALUES ('d1', 'no-such-incident', ?)",
                (iso(EVENT_TIME),),
            )


def test_failed_write_rolls_back_the_whole_run(db):
    """A collector run is a snapshot; the assembler cannot distinguish a half-written
    one from a quiet cluster, so partial writes must not survive."""
    with pytest.raises(sqlite3.IntegrityError):
        with lz.connect() as conn:
            run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
            lz.write_signals(conn, run, [make_signal()])
            conn.execute(
                "INSERT INTO diagnoses (diagnosis_id, incident_id, created_at) "
                "VALUES ('d1', 'no-such-incident', ?)",
                (iso(EVENT_TIME),),
            )

    with lz.connect() as conn:
        signals = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
        runs = conn.execute("SELECT COUNT(*) AS n FROM collection_runs").fetchone()

    assert signals["n"] == 0
    assert runs["n"] == 0


# ---- availability ----------------------------------------------------------


def test_unavailable_source_is_recorded_without_any_signals(db):
    """The reason collection_runs exists. A dead Prometheus produces zero signals, so
    without this row its outage is indistinguishable from a healthy quiet cluster."""
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.PROMETHEUS, CLUSTER)
        lz.finish_run(conn, run, SourceStatus.UNAVAILABLE, 0, error="connection refused")

    with lz.connect() as conn:
        row = lz.last_run(conn, "prometheus")

    assert row["status"] == "unavailable"
    assert row["signal_count"] == 0
    assert row["error"] == "connection refused"


# ---- recurrence ------------------------------------------------------------


def test_recurrences_group_without_losing_spacing(db):
    """Grouping happens at read time precisely so this test can pass: the gaps double,
    which is CrashLoopBackOff backing off. first/last/count alone cannot show that."""
    base = datetime(2026, 8, 5, 3, 0, 0, tzinfo=UTC)
    offsets = [0, 10, 30, 70, 150]
    signals = [make_signal(event_time=base + timedelta(seconds=o)) for o in offsets]

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, signals)

    window_start = base - timedelta(minutes=1)
    window_end = base + timedelta(minutes=10)

    with lz.connect() as conn:
        groups = lz.recurrences(conn, CLUSTER, window_start, window_end)
        times = lz.occurrence_times(conn, signals[0].fingerprint, window_start, window_end)

    assert len(groups) == 1
    assert groups[0]["occurrences"] == 5
    assert groups[0]["first_seen"] == iso(base)
    assert groups[0]["last_seen"] == iso(base + timedelta(seconds=150))

    parsed = [parse(t) for t in times]
    gaps = [int((b - a).total_seconds()) for a, b in zip(parsed, parsed[1:])]
    assert gaps == [10, 20, 40, 80]


def test_first_seen_ever_looks_past_the_incident_window(db):
    """Distinguishes "new since the deploy" from merely "after the deploy"."""
    old = make_signal(event_time=EVENT_TIME - timedelta(days=20))
    recent = make_signal(event_time=EVENT_TIME)

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [old, recent])

    with lz.connect() as conn:
        assert lz.first_seen_ever(conn, recent.fingerprint) == iso(old.event_time)
        assert lz.first_seen_ever(conn, "never-collected") is None


# ---- retention -------------------------------------------------------------


def test_sweep_deletes_expired_but_exempts_incident_evidence(db):
    loose = make_signal(name="loose-pod")
    kept = make_signal(name="kept-pod")

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [loose, kept])
        incident = lz.open_incident(conn, CLUSTER, NAMESPACE, trigger="manual")
        conn.execute(
            "UPDATE signals SET incident_id = ? WHERE subject_name = ?",
            (incident, "kept-pod"),
        )

    far_future = datetime.now(UTC) + timedelta(days=400)

    with lz.connect() as conn:
        result = lz.sweep_expired(conn, now=far_future)
        remaining = conn.execute("SELECT subject_name FROM signals").fetchall()

    assert result["signals"] == 1
    assert [r["subject_name"] for r in remaining] == ["kept-pod"]


def test_expiry_diverges_by_kind(db):
    """log_excerpt expires in a day, deploy in thirty. The deploy TTL is what makes the
    first_seen_ever lookback possible weeks after the fact."""
    log = make_signal(kind=SignalKind.LOG_EXCERPT, name="log-pod")
    deploy = make_signal(kind=SignalKind.DEPLOY, name="deploy-subject")

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, CLUSTER)
        lz.write_signals(conn, run, [log, deploy])

    cutoff = datetime.now(UTC) + timedelta(days=2)

    with lz.connect() as conn:
        lz.sweep_expired(conn, now=cutoff)
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM signals").fetchall()]

    assert kinds == ["deploy"]
