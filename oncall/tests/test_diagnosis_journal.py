"""
Every diagnose call reaches the journal, and the journal says how it ended.

  - Records go through the one write path, landing_zone.record_diagnosis, so they ship
    to the store through the outbox like every other row. The store tests read them back
    from Postgres, not from the buffer, because a row that never ships is not journalled.
  - Failures are journalled and leave the incident open. An incident whose every attempt
    failed is undiagnosed, and the query looking for those must still find it.
  - An existing buffer gains the new columns in place. Rebuilding it would discard the
    unshipped rows it holds, and it holds those exactly when the store is down.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from oncall import config
from oncall import landing_zone as lz
from oncall.diagnosis.journal import journal
from oncall.diagnosis.schema import Diagnosis, DiagnosisRecord, Outcome
from oncall.landing_zone import outbox

CLUSTER = "test-cluster"
FP = "3f9c2a7be41d0c5a9e8f7b6a5c4d3e2f"

DIAGNOSIS = Diagnosis.model_validate({
    "summary": "New pods cannot pull their image.",
    "insufficient_evidence": False,
    "hypotheses": [{
        "rank": 1, "cause_class": "image_pull", "title": "Pull fails",
        "mechanism": "registry.invalid does not resolve", "confidence": "high",
        "supporting": [FP], "contradicting": [],
        "verify": [
            {"command": "kubectl describe pod x", "purpose": "events"},
            {"command": "kubectl rollout history deployment/x", "purpose": "when"},
        ],
    }],
})


def record(incident_id: str, outcome: Outcome = Outcome.OK, **changes) -> DiagnosisRecord:
    fields = {
        "diagnosis_id": f"d-{outcome}",
        "incident_id": incident_id,
        "created_at": datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
        "model": "us.anthropic.claude-sonnet-4-6",
        "prompt_version": "1",
        "bundle_sha256": "b" * 64,
        "prompt_sha256": "p" * 64,
        "outcome": outcome,
        "attempts": 1,
        "stop_reason": "tool_use",
        "input_tokens": 9088,
        "output_tokens": 1152,
        "latency_ms": 16697,
        "cost_usd": 0.044544,
        "diagnosis": DIAGNOSIS if outcome == Outcome.OK else None,
        "errors": [] if outcome == Outcome.OK else ["attempt 1: summary: Field required"],
    }
    fields.update(changes)
    return DiagnosisRecord(**fields)


def open_incident() -> str:
    with lz.connect() as conn:
        return lz.open_incident(conn, CLUSTER, "oncall-lab", trigger="manual")


def row(diagnosis_id: str) -> sqlite3.Row:
    with lz.connect() as conn:
        return conn.execute(
            "SELECT * FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
        ).fetchone()


def state(incident_id: str) -> str:
    with lz.connect() as conn:
        return conn.execute(
            "SELECT state FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()["state"]


# ---- buffer ----------------------------------------------------------------


def test_a_successful_diagnosis_is_journalled_whole(buffer):
    incident = open_incident()
    with lz.connect() as conn:
        did = journal(conn, record(incident))

    r = row(did)
    assert did == "d-ok"
    assert r["outcome"] == "ok"
    assert r["prompt_version"] == "1"
    assert (r["attempts"], r["stop_reason"]) == (1, "tool_use")
    assert r["cost_usd"] == pytest.approx(0.044544)
    assert r["summary"] == "New pods cannot pull their image."
    assert r["insufficient_evidence"] == 0
    assert json.loads(r["errors"]) == []
    assert json.loads(r["hypotheses"])[0]["supporting"] == [FP]
    assert json.loads(r["commands"]) == [
        {"rank": 1, "command": "kubectl describe pod x", "purpose": "events"},
        {"rank": 1, "command": "kubectl rollout history deployment/x", "purpose": "when"},
    ]
    assert state(incident) == "diagnosed"


@pytest.mark.parametrize(
    "outcome", [Outcome.INVALID_OUTPUT, Outcome.GUARD_REJECTED, Outcome.MODEL_ERROR,
                Outcome.PROMPT_TOO_LARGE]
)
def test_a_failed_attempt_is_journalled_and_leaves_the_incident_open(buffer, outcome):
    incident = open_incident()
    with lz.connect() as conn:
        did = journal(conn, record(incident, outcome))

    r = row(did)
    assert r["outcome"] == str(outcome)
    assert json.loads(r["errors"]) == ["attempt 1: summary: Field required"]
    assert json.loads(r["hypotheses"]) == []
    assert r["summary"] is None
    assert r["insufficient_evidence"] is None
    assert state(incident) == "open"


def test_a_failed_attempt_is_queued_for_the_store(buffer):
    incident = open_incident()
    with lz.connect() as conn:
        did = journal(conn, record(incident, Outcome.MODEL_ERROR))
        _, batch = lz.pending(conn, 100)

    assert batch[outbox.DIAGNOSIS] == [did]


def test_a_buffer_from_before_outcomes_gains_the_columns_in_place(tmp_path, monkeypatch):
    """The shape of buffer.db written by M4: diagnoses without any outcome column, one
    row already in it. bootstrap() must add the columns and keep the row."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "BUFFER_DB_PATH", tmp_path / "buffer.db")
    monkeypatch.setattr(config, "BLOB_DIR", tmp_path / "blobs")
    old = sqlite3.connect(tmp_path / "buffer.db")
    old.executescript("""
        CREATE TABLE incidents (incident_id TEXT PRIMARY KEY, opened_at TEXT NOT NULL,
            closed_at TEXT, cluster TEXT NOT NULL, namespace TEXT, trigger TEXT,
            title TEXT, state TEXT NOT NULL);
        CREATE TABLE diagnoses (diagnosis_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
            created_at TEXT NOT NULL, model TEXT, bundle_sha256 TEXT, hypotheses TEXT,
            commands TEXT, input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER);
        INSERT INTO incidents VALUES ('i1', '2026-09-30T00:00:00Z', NULL, 'c', NULL,
            'manual', NULL, 'diagnosed');
        INSERT INTO diagnoses (diagnosis_id, incident_id, created_at)
            VALUES ('old', 'i1', '2026-09-30T00:00:00Z');
    """)
    old.commit()
    old.close()

    lz.bootstrap()

    with lz.connect() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(diagnoses)")}
        kept = conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = 'old'").fetchone()

    assert {"outcome", "attempts", "errors", "cost_usd", "insufficient_evidence",
            "prompt_version", "stop_reason", "summary", "prompt_sha256", "unknowns"} <= columns
    assert kept["outcome"] is None
    assert kept["unknowns"] is None


def test_unknowns_are_journalled_as_the_model_wrote_them(buffer):
    """The diagnosis's own statement of what it could not see. Dropped, a stored answer
    reads as more certain than the one the engineer was shown."""
    incident = open_incident()
    stated = DIAGNOSIS.model_copy(update={"unknowns": ["k8s_logs unavailable", "no metrics"]})
    with lz.connect() as conn:
        ok = journal(conn, record(incident, diagnosis=stated))
        failed = journal(conn, record(incident, Outcome.MODEL_ERROR))

    assert json.loads(row(ok)["unknowns"]) == ["k8s_logs unavailable", "no metrics"]
    assert row(failed)["unknowns"] is None


def test_no_unknowns_is_an_empty_list_not_a_null(buffer):
    incident = open_incident()
    with lz.connect() as conn:
        did = journal(conn, record(incident))

    assert json.loads(row(did)["unknowns"]) == []


# ---- store -----------------------------------------------------------------


def test_both_outcomes_reach_the_store_and_read_back(buffer, store_db):
    from oncall import shipper
    from oncall.store import reader

    incident = open_incident()
    with lz.connect() as conn:
        journal(conn, record(incident, Outcome.INVALID_OUTPUT,
                             created_at=datetime(2026, 10, 4, 11, 59, tzinfo=UTC)))
        journal(conn, record(incident, Outcome.OK))

    shipper.ship_all()

    with store_db.connect() as conn:
        rows = reader.diagnoses_for_incident(conn, incident)
        inc = store_db.fetch_one(conn, "SELECT state FROM incidents")

    failed, ok = rows
    assert [failed["outcome"], ok["outcome"]] == ["invalid_output", "ok"]
    assert failed["errors"] == ["attempt 1: summary: Field required"]
    assert failed["insufficient_evidence"] is None
    assert ok["insufficient_evidence"] is False
    assert ok["cost_usd"] == Decimal("0.044544")
    assert ok["hypotheses"][0]["cause_class"] == "image_pull"
    assert ok["prompt_version"] == "1"
    assert ok["unknowns"] == []
    assert failed["unknowns"] is None
    assert inc["state"] == "diagnosed"


def test_an_incident_whose_attempts_all_failed_stays_open_in_the_store(buffer, store_db):
    from oncall import shipper

    incident = open_incident()
    with lz.connect() as conn:
        journal(conn, record(incident, Outcome.MODEL_ERROR))

    shipper.ship_all()

    with store_db.connect() as conn:
        inc = store_db.fetch_one(conn, "SELECT state FROM incidents")
        diag = store_db.fetch_one(conn, "SELECT outcome, cost_usd FROM diagnoses")

    assert inc["state"] == "open"
    assert diag["outcome"] == "model_error"


def test_an_incident_shipped_open_is_shipped_again_once_diagnosed(buffer, store_db):
    """The usual order in production: the incident reaches the store while still open,
    and the diagnosis arrives minutes later. Only the re-queue on success tells the store
    the state moved; journalling before the first ship would hide its absence, because
    the one shipment would already carry 'diagnosed'."""
    from oncall import shipper

    incident = open_incident()
    shipper.ship_all()
    with store_db.connect() as conn:
        assert store_db.fetch_one(conn, "SELECT state FROM incidents")["state"] == "open"

    with lz.connect() as conn:
        journal(conn, record(incident))
    shipper.ship_all()

    with store_db.connect() as conn:
        assert store_db.fetch_one(conn, "SELECT state FROM incidents")["state"] == "diagnosed"


def test_a_prompt_too_large_record_ships_with_no_prompt_receipt(buffer, store_db):
    """Nothing was sent, so there is no prompt receipt, no tokens and no stop reason.
    The row still ships: it is what a 'prompt too large' rate is counted from."""
    from oncall import shipper

    incident = open_incident()
    with lz.connect() as conn:
        journal(conn, record(incident, Outcome.PROMPT_TOO_LARGE, prompt_sha256=None,
                             attempts=0, stop_reason=None, input_tokens=0,
                             output_tokens=0, latency_ms=0, cost_usd=None,
                             errors=["evidence is 130000 bytes, over the prompt cap of 122880"]))

    shipper.ship_all()

    with store_db.connect() as conn:
        diag = store_db.fetch_one(
            conn, "SELECT outcome, prompt_sha256, attempts, cost_usd FROM diagnoses"
        )
        inc = store_db.fetch_one(conn, "SELECT state FROM incidents")

    assert dict(diag) == {"outcome": "prompt_too_large", "prompt_sha256": None,
                          "attempts": 0, "cost_usd": None}
    assert inc["state"] != "diagnosed"
