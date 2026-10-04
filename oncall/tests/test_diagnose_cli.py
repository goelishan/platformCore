"""
make diagnose end to end: the incident it opens, the snapshot it keeps, the record it
journals, and a replay that runs the same evidence again.

  - The buffer is real and Bedrock is the fake client from the reasoner tests, so every
    path runs except the network call.
  - Replay is judged on the property it exists for: the evidence it ran is byte for byte
    the evidence the original diagnosis saw, and a tampered snapshot is refused.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest

from oncall import config
from oncall import landing_zone as lz
from oncall.diagnosis import snapshots
from oncall.envelope import Severity
from oncall.evidence import bundle
from oncall.tests import test_bundle as tb
from oncall.tests import test_diagnosis_reasoner as tr

_spec = importlib.util.spec_from_file_location(
    "diagnose_cli", Path(__file__).resolve().parents[1] / "dev" / "diagnose.py"
)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

ARGS = [tb.POD, "--cluster", tb.CLUSTER]


@pytest.fixture
def lab(buffer, monkeypatch):
    """The model id is pinned here, not read from oncall/.env, so these tests mean the
    same thing on a laptop with Bedrock configured and in CI without it."""
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", tr.MODEL)
    tb.store([
        tb.make_signal(when=tb.NOW - timedelta(minutes=2), key="app|Pulled|",
                       severity=Severity.INFO, payload={"reason": "Pulled"}),
        tb.make_signal(when=tb.NOW - timedelta(minutes=8), key="app|ErrImagePull|",
                       severity=Severity.ERROR, payload={"reason": "ErrImagePull"}),
    ])


def incidents() -> list[dict]:
    with lz.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM incidents")]


def diagnoses() -> list[dict]:
    with lz.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM diagnoses ORDER BY created_at")]


# ---- the landing zone pieces -----------------------------------------------


def test_attach_by_id_takes_exactly_the_given_signals(lab):
    with lz.connect() as conn:
        ids = [r["signal_id"] for r in conn.execute("SELECT signal_id FROM signals")]
        incident = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual")
        n = lz.attach_signal_ids(conn, incident, ids[:1])
        attached = [r["signal_id"] for r in conn.execute(
            "SELECT signal_id FROM signals WHERE incident_id = ?", (incident,))]

    assert n == 1
    assert attached == ids[:1]


def test_attach_by_id_leaves_a_signal_with_its_first_incident(lab):
    with lz.connect() as conn:
        ids = [r["signal_id"] for r in conn.execute("SELECT signal_id FROM signals")]
        first = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual")
        second = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual")
        lz.attach_signal_ids(conn, first, ids)

        assert lz.attach_signal_ids(conn, second, ids) == 0


def test_attach_by_id_crosses_the_parameter_chunk(lab):
    """More ids than one chunk holds, real ones at both ends, so a loop that stopped after
    the first chunk or skipped its tail would miss one."""
    with lz.connect() as conn:
        ids = [r["signal_id"] for r in conn.execute("SELECT signal_id FROM signals")]
        incident = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual")
        padded = [ids[0], *(f"missing-{i}" for i in range(1200)), ids[1]]

        assert lz.attach_signal_ids(conn, incident, padded) == 2


def test_an_unresolved_incident_is_found_for_the_same_subject_only(buffer):
    """The resolved incident is the newest one here. Opened first, it would lose to the
    open one on recency alone and a query that forgot to exclude resolved incidents
    would still pass."""
    with lz.connect() as conn:
        current = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual", title="api")
        newest = lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual", title="api")
        lz.close_incident(conn, newest)
        lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual", title="other")
        lz.open_incident(conn, "elsewhere", "prod", trigger="manual", title="api")

        assert lz.unresolved_incident(conn, tb.CLUSTER, "api") == current
        assert lz.unresolved_incident(conn, tb.CLUSTER, None) is None


# ---- snapshots -------------------------------------------------------------


def test_a_snapshot_round_trips_to_the_same_receipt(lab):
    original = tb.build()
    path = snapshots.save(original)

    loaded = snapshots.load(bundle.evidence_receipt(original))

    assert path.name == f"{bundle.evidence_receipt(original)}.json"
    assert bundle.render(loaded) == bundle.render(original)
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_a_tampered_snapshot_is_refused(lab):
    original = tb.build()
    path = snapshots.save(original)
    data = json.loads(path.read_text())
    data["findings"][0]["facts"]["reason"] = "OOMKilled"
    path.write_text(json.dumps(data))

    with pytest.raises(snapshots.SnapshotMismatch):
        snapshots.load(bundle.evidence_receipt(original))


def test_a_missing_snapshot_says_where_it_looked(buffer):
    with pytest.raises(FileNotFoundError, match="no snapshot for bundle abc"):
        snapshots.load("abc")


# ---- the command -----------------------------------------------------------


def test_a_dry_run_prints_the_prompt_and_writes_nothing(lab, capsys):
    assert cli.main([*ARGS, "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "<evidence>" in out and "F1  [error" in out
    assert "bundle_sha256" in out and "prompt_sha256" in out
    assert "Bedrock not called" in out
    assert incidents() == []
    assert diagnoses() == []
    assert not snapshots.directory().exists()


def test_a_live_run_opens_attaches_snapshots_and_journals(lab, capsys):
    client = tr.FakeBedrock(tr.answer(tr.diagnosis()))

    assert cli.main(ARGS, client=client) == 0

    [incident] = incidents()
    [record] = diagnoses()
    with lz.connect() as conn:
        attached = conn.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE incident_id = ?",
            (incident["incident_id"],),
        ).fetchone()["n"]

    assert incident["title"] == tb.POD and incident["state"] == "diagnosed"
    assert attached == 2
    assert record["outcome"] == "ok"
    assert snapshots.path_for(record["bundle_sha256"]).exists()
    out = capsys.readouterr().out
    assert "opened" in out and "supported by F1" in out


def test_a_second_run_reuses_the_incident(lab):
    cli.main(ARGS, client=tr.FakeBedrock(tr.answer(tr.diagnosis())))
    cli.main(ARGS, client=tr.FakeBedrock(tr.answer(tr.diagnosis())))

    assert len(incidents()) == 1
    assert len(diagnoses()) == 2


def test_a_failed_run_is_journalled_and_exits_non_zero(lab):
    assert cli.main(ARGS, client=tr.FakeBedrock(tr.throttled())) == 1

    [record] = diagnoses()
    assert record["outcome"] == "model_error"
    assert incidents()[0]["state"] == "open"


def test_replay_runs_the_journalled_evidence_and_journals_again(lab, capsys):
    cli.main(ARGS, client=tr.FakeBedrock(tr.answer(tr.diagnosis())))
    [first] = diagnoses()

    # New evidence arrives after the original diagnosis. Replay must not see it.
    tb.store([tb.make_signal(when=tb.NOW - timedelta(minutes=1), key="app|OOMKilled|137",
                             payload={"reason": "OOMKilled"})])
    capsys.readouterr()
    client = tr.FakeBedrock(tr.answer(tr.diagnosis()))

    assert cli.main(["--replay", first["diagnosis_id"]], client=client) == 0

    second = diagnoses()[1]
    assert second["bundle_sha256"] == first["bundle_sha256"]
    assert second["incident_id"] == first["incident_id"]
    assert "OOMKilled" not in client.requests[0]["messages"][0]["content"][0]["text"]
    out = capsys.readouterr().out
    assert "receipt verified" in out and "image_pull (high)" in out


def test_replay_refuses_a_tampered_snapshot(lab):
    cli.main(ARGS, client=tr.FakeBedrock(tr.answer(tr.diagnosis())))
    [first] = diagnoses()
    path = snapshots.path_for(first["bundle_sha256"])
    path.write_text(path.read_text().replace("ErrImagePull", "OOMKilled"))

    with pytest.raises(snapshots.SnapshotMismatch):
        cli.main(["--replay", first["diagnosis_id"]], client=tr.FakeBedrock())


def test_a_cluster_wide_run_reuses_only_a_cluster_wide_incident(buffer):
    """title IS NULL, not title = NULL: the second never matches, and every cluster-wide
    run would open a fresh incident."""
    with lz.connect() as conn:
        lz.open_incident(conn, tb.CLUSTER, "prod", trigger="manual", title="api")
        whole = lz.open_incident(conn, tb.CLUSTER, None, trigger="manual", title=None)

        assert lz.unresolved_incident(conn, tb.CLUSTER, None) == whole
