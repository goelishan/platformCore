"""
The Streamlit app, run headless against a real buffer with Bedrock faked.

  - AppTest executes ui/app.py in this process, so the buffer fixture and the fake client
    are what the app sees. Every path runs except the network call.
  - Asserted on what an engineer would read: the subject offered, the first command to
    run, the rejection banner, the history row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from oncall import config
from oncall.diagnosis import bedrock, service
from oncall.envelope import Severity
from oncall.tests import test_bundle as tb
from oncall.tests import test_diagnosis_reasoner as tr

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")


@pytest.fixture(autouse=True)
def _no_store():
    """The app falls back to the store for history; these tests are about the buffer."""
    service._store_skipped_until = float("inf")
    yield
    service._store_skipped_until = 0.0


@pytest.fixture
def lab(buffer, monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", tr.MODEL)
    monkeypatch.setattr(config, "CLUSTER_NAME", tb.CLUSTER)
    now = datetime.now(UTC)
    tb.store([
        tb.make_signal(when=now - timedelta(minutes=8), key="app|ErrImagePull|",
                       severity=Severity.ERROR, payload={"reason": "ErrImagePull"}),
    ])


def fake(monkeypatch, *answers):
    client = tr.FakeBedrock(*answers)
    monkeypatch.setattr(bedrock, "default_client", lambda: client)
    return client


def run() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def subject(at: AppTest, name: str) -> None:
    next(t for t in at.text_input if t.label.startswith("Or type")).input(name).run()


def click(at: AppTest, label: str) -> None:
    next(b for b in at.button if b.label == label).click().run()


def codes(at: AppTest) -> list[str]:
    return [c.value for c in at.code]


def test_the_app_starts_on_an_empty_buffer(buffer, monkeypatch):
    monkeypatch.setattr(config, "CLUSTER_NAME", tb.CLUSTER)
    at = run()

    assert any("No collection has run" in w.value for w in at.sidebar.warning)


def test_a_failing_workload_is_offered_as_a_subject(lab):
    at = run()

    options = at.selectbox[0].options
    assert options[0] == "(whole cluster)"
    assert [o.split("  ·  ")[0] for o in options[1:]] == [tb.POD]


def test_diagnose_shows_the_first_command_and_journals_it(lab, monkeypatch):
    client = fake(monkeypatch, tr.answer(tr.diagnosis()))
    at = run()
    subject(at, tb.POD)
    click(at, "Diagnose")

    assert not at.exception, at.exception
    assert len(client.requests) == 1
    assert "kubectl describe pod x" in codes(at)
    assert any(m.value == "**Run first**" for m in at.markdown)
    assert [r["outcome"] for r in service.recent()] == ["ok"]
    # The remedy is shown with its risk first, its command to copy and its undo.
    assert tr.REMEDY["command"] in codes(at)
    assert any(w.value.startswith("medium risk: ") for w in at.warning)
    assert any(c.value.startswith("Undo: ") for c in at.caption)


def test_an_unsafe_answer_is_a_banner_not_a_diagnosis(lab, monkeypatch):
    fake(monkeypatch, tr.answer(tr.diagnosis(command="kubectl delete pod x")))
    at = run()
    subject(at, tb.POD)
    click(at, "Diagnose")

    assert any("allowlist" in e.value for e in at.error)
    assert not any(m.value == "**Run first**" for m in at.markdown)


def test_preview_calls_nothing_and_writes_nothing(lab, monkeypatch):
    client = fake(monkeypatch)
    at = run()
    subject(at, tb.POD)
    click(at, "Preview evidence")

    assert len(client.requests) == 0
    assert any("Nothing has been sent" in i.value for i in at.info)
    assert service.recent() == []


def test_history_reopens_a_diagnosis_from_its_snapshot(lab, monkeypatch):
    fake(monkeypatch, tr.answer(tr.diagnosis()))
    at = run()
    subject(at, tb.POD)
    click(at, "Diagnose")

    fresh = run()
    assert len(fresh.dataframe) == 1
    assert "kubectl describe pod x" in codes(fresh)
    assert not any("No bundle snapshot" in t.value for t in fresh.text)


def test_with_diagnose_off_the_button_is_disabled_and_says_why(lab, monkeypatch):
    client = fake(monkeypatch)
    monkeypatch.setattr(config, "DIAGNOSE_ENABLED", False)
    at = run()

    button = next(b for b in at.button if b.label == "Diagnose")
    assert button.disabled
    assert any("no AWS identity" in c.value for c in at.caption)
    assert len(client.requests) == 0
