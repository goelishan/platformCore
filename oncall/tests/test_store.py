"""
Store contract tests.

Everything here needs a real Postgres, because everything here is a feature SQLite does
not have: jsonb containment, partitioned tables, catalogue introspection. Testing them
against a stand-in would test the stand-in. They skip when no store is reachable — a
missing dev container is not a broken codebase.

    docker compose -f oncall/dev/compose.yaml up -d
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# Before any import that reaches the store package, which pulls in psycopg at module
# level. A missing driver is a collection error, not a skip, and a collection error
# fails the entire run — including every test that has nothing to do with the store.
# The application genuinely requires psycopg; the test run should not require it to
# exercise the hundred cases that never touch a database.
pytest.importorskip("psycopg_pool", reason="psycopg not installed; pip install -r oncall/requirements.txt")

from oncall.envelope import Owner, Signal, SignalKind, SignalSource, Subject, iso  # noqa: E402
from oncall.landing_zone.rows import to_row
from oncall.store import rows as store_rows

CLUSTER = "test-cluster"
T0 = datetime(2026, 8, 12, 3, 14, 0, tzinfo=UTC)


def make_signal(
    name: str = "api-7f9",
    when: datetime = T0,
    key: str = "app|Error|1",
    payload: dict | None = None,
    kind: SignalKind = SignalKind.POD_STATE,
) -> Signal:
    return Signal(
        source=SignalSource.K8S_PODS,
        kind=kind,
        cluster=CLUSTER,
        namespace="prod",
        event_time=when,
        subject=Subject(kind="Pod", name=name),
        owner=Owner(kind="Deployment", name="api"),
        node="node-1",
        dedupe_key=key,
        payload=payload if payload is not None else {"exit_code": 1, "container": "app"},
    )


def as_store_row(signal: Signal, incident_id: str | None = None) -> dict:
    """Through the buffer's row shape first, exactly as the shipper does. Converting
    straight from a Signal would skip the text-to-type conversions that are the whole
    job of store.rows, and would test a path nothing uses."""
    row = to_row(signal, run_id=None)
    row["incident_id"] = incident_id
    return store_rows.signal_from_buffer(row)


def incident_row(incident_id: str = "inc-1", state: str = "open") -> dict:
    return {
        "incident_id": incident_id,
        "opened_at": T0,
        "closed_at": None,
        "cluster": CLUSTER,
        "namespace": "prod",
        "trigger": "manual",
        "title": None,
        "state": state,
    }


# ---- migrations ------------------------------------------------------------


def test_migrations_are_idempotent(store_db):
    """Safe to call on every start, which is deliberate: a deploy that forgets to run
    migrations is a class of outage worth designing out rather than documenting."""
    assert store_db.migrate() == []
    assert store_db.pending() == []


# ---- partitions ------------------------------------------------------------


def test_writing_creates_the_partition_it_needs(store_db):
    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(make_signal())])
        names = {p["name"] for p in store_db.signal_partitions(conn)}

    assert "signals_2026_08" in names


def test_a_drained_backlog_creates_partitions_for_older_months(store_db):
    """The reason partitions are created from the data rather than on a schedule. A
    backlog that drained after a long store outage carries rows from a previous month,
    and a 'create next month' job would have no reason to have made that partition —
    inserting with no matching partition is a hard error, so the batch would fail
    permanently and retry forever."""
    old = make_signal(name="old", when=T0 - timedelta(days=90), key="app|Error|old")

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(old), as_store_row(make_signal())])
        names = {p["name"] for p in store_db.signal_partitions(conn)}

    assert {"signals_2026_05", "signals_2026_08"} <= names


# ---- idempotent arrival ----------------------------------------------------


def test_reshipping_a_batch_writes_no_duplicates(store_db):
    """What turns the shipper's at-least-once delivery into effectively-once arrival.
    A crash between the store commit and clearing the outbox re-sends the batch, and
    that has to be the ordinary recovery path rather than a corruption event."""
    row = as_store_row(make_signal())

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [row])
        store_db.upsert_signals(conn, [row])
        count = store_db.fetch_one(conn, "SELECT COUNT(*) AS n FROM signals")

    assert count["n"] == 1


def test_a_later_ship_carries_the_incident_id_across(store_db):
    """incident_id is back-filled long after a signal first ships. It is also what
    exempts a signal from retention as eval corpus, so a store that never learns about
    it deletes evidence it was supposed to keep."""
    signal = make_signal()

    with store_db.connect() as conn:
        store_db.upsert_incidents(conn, [incident_row()])
        store_db.upsert_signals(conn, [as_store_row(signal)])
        store_db.upsert_signals(conn, [as_store_row(signal, incident_id="inc-1")])
        stored = store_db.fetch_one(conn, "SELECT incident_id FROM signals")

    assert stored["incident_id"] == "inc-1"


def test_identity_is_not_widened_by_the_composite_key(store_db):
    """event_time is in the primary key only because Postgres requires the partition key
    inside a unique constraint. signal_id is a hash of fingerprint and event_time, so
    the pair can never disagree and the conflict target behaves as if keyed on
    signal_id alone."""
    signal = make_signal()

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(signal)])
        stored = store_db.fetch_one(conn, "SELECT signal_id, event_time FROM signals")

    assert stored["signal_id"] == signal.signal_id
    assert iso(stored["event_time"]) == iso(signal.event_time)


# ---- what the buffer cannot answer -----------------------------------------


def test_first_seen_ever_looks_past_the_buffer_window(store_db):
    """The query the store exists for. Asked against a two-day buffer it would answer
    "never seen before" for anything older, which is the most confident possible way to
    be wrong."""
    old = make_signal(when=T0 - timedelta(days=20))
    recent = make_signal(when=T0)

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(old), as_store_row(recent)])
        first = store_db.first_seen_ever(conn, recent.fingerprint)
        missing = store_db.first_seen_ever(conn, "never-collected")

    assert iso(first) == iso(old.event_time)
    assert missing is None


def test_is_new_distinguishes_new_from_newly_noticed(store_db):
    old = make_signal(when=T0 - timedelta(days=20))

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(old)])
        assert not store_db.is_new(conn, old.fingerprint, T0)
        assert store_db.is_new(conn, old.fingerprint, T0 - timedelta(days=30))


def test_one_fingerprint_spans_clusters(store_db):
    """A fingerprint carries no cluster, so "is this happening elsewhere too" is a plain
    grouping. That is the payoff for deriving identity from content rather than from
    location."""
    here = make_signal()
    there = make_signal()
    row = as_store_row(there)
    row["cluster"] = "other-cluster"
    # signal_id must differ or the two collapse; in reality the cluster is inside the
    # fingerprint basis, so this mirrors two genuinely separate observations.
    row["signal_id"] = row["signal_id"][:-1] + "z"

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(here), row])
        spread = store_db.clusters_affected(
            conn, here.fingerprint, T0 - timedelta(days=1), T0 + timedelta(days=1)
        )

    assert {r["cluster"] for r in spread} == {CLUSTER, "other-cluster"}


def test_recurrence_history_buckets_by_day(store_db):
    rows = [
        as_store_row(make_signal(when=T0 - timedelta(days=d), key="app|Error|1"))
        for d in range(3)
    ]

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, rows)
        history = store_db.recurrence_history(
            conn,
            make_signal().fingerprint,
            T0 - timedelta(days=10),
            T0 + timedelta(days=1),
        )

    assert len(history) == 3
    assert all(r["occurrences"] == 1 for r in history)


# ---- jsonb -----------------------------------------------------------------


def test_payload_is_queryable_not_opaque(store_db):
    """The reason payload is jsonb rather than text. In the buffer this question is a
    full scan of every payload; here it is an index lookup, and it can filter on fields
    the schema never knew about."""
    oom = make_signal(name="oom", key="app|OOMKilled|137",
                      payload={"exit_code": 137, "reason": "OOMKilled"})
    err = make_signal(name="err", key="app|Error|1",
                      payload={"exit_code": 1, "reason": "Error"})

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(oom), as_store_row(err)])
        hits = store_db.signals_by_payload(
            conn, CLUSTER, T0 - timedelta(days=1), T0 + timedelta(days=1),
            {"reason": "OOMKilled"},
        )

    assert [h["subject_name"] for h in hits] == ["oom"]


def test_payload_arrives_as_a_document_not_a_string(store_db):
    """Jsonb() rather than passing the serialised text through. Without it the store
    would hold a JSON document that happens to be a quoted string, every containment
    query would miss, and nothing would error."""
    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(make_signal())])
        typed = store_db.fetch_one(
            conn, "SELECT jsonb_typeof(payload) AS t, payload->>'container' AS c FROM signals"
        )

    assert typed["t"] == "object"
    assert typed["c"] == "app"


# ---- retention -------------------------------------------------------------


def test_expired_partitions_are_dropped_whole(store_db):
    """Dropping a partition is a metadata operation that reclaims space instantly; the
    equivalent DELETE has to be vacuumed afterwards. This is the delete-heavy pattern
    that motivated moving off SQLite."""
    ancient = make_signal(when=T0 - timedelta(days=400), key="app|Error|ancient")

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(ancient), as_store_row(make_signal())])
        dropped = store_db.drop_expired_partitions(conn, now=T0)
        names = {p["name"] for p in store_db.signal_partitions(conn)}

    assert "signals_2025_07" in dropped
    assert "signals_2026_08" in names


def test_a_partition_holding_incident_evidence_is_kept(store_db):
    """Those rows are the eval corpus. Losing them destroys the ability to measure
    whether diagnoses are getting better, which is the one thing that makes the reasoner
    improvable rather than merely present."""
    ancient = make_signal(when=T0 - timedelta(days=400), key="app|Error|ancient")

    with store_db.connect() as conn:
        store_db.upsert_incidents(conn, [incident_row()])
        store_db.upsert_signals(conn, [as_store_row(ancient, incident_id="inc-1")])
        dropped = store_db.drop_expired_partitions(conn, now=T0)

    assert dropped == []


def test_row_level_expiry_runs_inside_live_partitions(store_db):
    """A partition spans a month while a log excerpt lives a day. Without the row-level
    sweep, January's log excerpts would survive until February."""
    log = make_signal(name="log", kind=SignalKind.LOG_EXCERPT, key="app|log|1")
    pod = make_signal(name="pod")

    with store_db.connect() as conn:
        store_db.upsert_signals(conn, [as_store_row(log), as_store_row(pod)])
        store_db.sweep(conn, now=datetime.now(UTC) + timedelta(days=2))
        kinds = [r["kind"] for r in store_db.fetch_all(conn, "SELECT kind FROM signals")]

    assert kinds == ["pod_state"]
