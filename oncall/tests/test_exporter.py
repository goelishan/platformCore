"""
The journal exporter reports what the store holds, and says so when it cannot.

  - Rows reach the store through the real path: journal, outbox, shipper. An exporter
    tested against hand-inserted rows would be tested against the test's idea of the
    schema rather than the one the shipper writes.
  - Output is parsed back with prometheus_client's own text parser, so a family that
    renders invalidly fails here rather than in a scrape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from oncall import landing_zone as lz
from oncall.diagnosis.journal import journal
from oncall.diagnosis.schema import Outcome
from oncall.exporter import journal as exporter
from oncall.tests.test_diagnosis_journal import open_incident, record

MODEL = "us.anthropic.claude-sonnet-4-6"
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def scrape() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    registry = CollectorRegistry()
    registry.register(exporter.JournalCollector())
    text = generate_latest(registry).decode()
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


def value(samples, name: str, **labels: str) -> float:
    return samples[(name, tuple(sorted(labels.items())))]


def names(samples) -> set[str]:
    return {name for name, _ in samples}


def ship(*records) -> None:
    from oncall import shipper

    incident = open_incident()
    with lz.connect() as conn:
        for i, r in enumerate(records):
            journal(conn, r.model_copy(update={
                "incident_id": incident, "diagnosis_id": f"d{i}",
                "created_at": T0 + timedelta(minutes=i),
            }))
    shipper.ship_all()


@pytest.fixture
def journalled(buffer, store_db, monkeypatch):
    monkeypatch.setattr(exporter.config, "BEDROCK_MODEL_ID", MODEL)
    ship(
        record("x", Outcome.OK, latency_ms=16_000, attempts=1),
        record("x", Outcome.OK, latency_ms=34_000, attempts=2,
               input_tokens=13_000, output_tokens=1_700, cost_usd=0.064),
        record("x", Outcome.GUARD_REJECTED, latency_ms=28_000, attempts=2),
        record("x", Outcome.OK, latency_ms=9_000, attempts=1, cost_usd=None,
               prompt_version="5"),
        record("x", Outcome.PROMPT_TOO_LARGE, latency_ms=0, attempts=0, prompt_sha256=None,
               input_tokens=0, output_tokens=0, cost_usd=None),
    )
    return scrape()


def test_every_diagnosis_is_counted_by_outcome(journalled):
    assert value(journalled, "oncall_diagnoses_total", model=MODEL, prompt_version="1",
                 outcome="ok") == 2
    assert value(journalled, "oncall_diagnoses_total", model=MODEL, prompt_version="1",
                 outcome="guard_rejected") == 1
    assert value(journalled, "oncall_diagnoses_total", model=MODEL, prompt_version="1",
                 outcome="prompt_too_large") == 1
    assert value(journalled, "oncall_diagnoses_total", model=MODEL, prompt_version="5",
                 outcome="ok") == 1


def test_outcomes_never_seen_are_present_at_zero(journalled):
    """Without these, the first model_error ever recorded would be invisible to
    increase(), which needs a prior sample to measure a rise from."""
    for outcome in Outcome:
        key = ("oncall_diagnoses_total", tuple(sorted(
            {"model": MODEL, "prompt_version": "5", "outcome": outcome.value}.items())))
        assert key in journalled
    assert value(journalled, "oncall_diagnoses_total", model=MODEL, prompt_version="5",
                 outcome="model_error") == 0


def test_repairs_and_first_attempt_success_are_separate(journalled):
    assert value(journalled, "oncall_diagnoses_first_attempt_ok_total",
                 model=MODEL, prompt_version="1") == 1
    assert value(journalled, "oncall_diagnosis_attempts_total",
                 model=MODEL, prompt_version="1") == 5


def test_latency_is_a_cumulative_histogram_of_calls_actually_made(journalled):
    """prompt_too_large sent nothing; its latency of 0 would drag every quantile down."""
    count = value(journalled, "oncall_diagnosis_latency_seconds_count", model=MODEL)
    assert count == 4
    assert value(journalled, "oncall_diagnosis_latency_seconds_sum", model=MODEL) == 87
    le = lambda b: value(journalled, "oncall_diagnosis_latency_seconds_bucket",  # noqa: E731
                         model=MODEL, le=b)
    assert le("5.0") == 0
    assert le("10.0") == 1
    assert le("20.0") == 2
    assert le("30.0") == 3
    assert le("45.0") == 4
    assert le("+Inf") == count


def test_cost_counts_only_priced_calls_and_counts_the_rest_separately(journalled):
    """An unpriced call is an absent figure, never a zero, as in the journal itself."""
    assert value(journalled, "oncall_diagnosis_cost_usd_total", model=MODEL) == pytest.approx(
        0.044544 + 0.064 + 0.044544)
    assert value(journalled, "oncall_diagnoses_unpriced_total", model=MODEL) == 1


def test_tokens_and_the_newest_diagnosis_are_reported(journalled):
    assert value(journalled, "oncall_diagnosis_tokens_total", model=MODEL,
                 direction="input") == 9088 * 3 + 13_000
    assert value(journalled, "oncall_diagnosis_last_timestamp_seconds", model=MODEL) == (
        (T0 + timedelta(minutes=3)).timestamp())


def test_a_readable_store_reports_up(journalled):
    assert value(journalled, "oncall_journal_up") == 1


def test_counters_do_not_reset_when_the_exporter_restarts(journalled):
    """A fresh collector reads the same table and resumes at the same totals."""
    again = scrape()
    assert {k: v for k, v in again.items() if "duration" not in k[0]} == {
        k: v for k, v in journalled.items() if "duration" not in k[0]}


def test_an_empty_journal_with_no_model_configured_has_no_journal_series(
        buffer, store_db, monkeypatch):
    monkeypatch.setattr(exporter.config, "BEDROCK_MODEL_ID", None)
    samples = scrape()

    assert value(samples, "oncall_journal_up") == 1
    assert not any(n.startswith("oncall_diagnos") for n in names(samples))


def test_the_configured_model_and_current_prompt_exist_at_zero_before_any_row(
        buffer, store_db, monkeypatch):
    """So the first diagnosis under a new prompt version or model is a rise from zero
    that increase() can see, not the birth of a series that it cannot."""
    monkeypatch.setattr(exporter.config, "BEDROCK_MODEL_ID", "new-model")
    samples = scrape()
    version = exporter.PROMPT_VERSION

    for outcome in Outcome:
        assert value(samples, "oncall_diagnoses_total", model="new-model",
                     prompt_version=version, outcome=outcome.value) == 0
    assert value(samples, "oncall_diagnosis_latency_seconds_count", model="new-model") == 0
    assert value(samples, "oncall_diagnosis_cost_usd_total", model="new-model") == 0
    assert ("oncall_diagnosis_last_timestamp_seconds", (("model", "new-model"),)) not in samples


def test_an_unreachable_store_is_down_and_never_reads_as_zero_failures(monkeypatch):
    def refuse(*_a, **_k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(exporter.connection, "connect", refuse)
    monkeypatch.setattr(exporter.connection, "fresh_login", refuse)
    samples = scrape()

    assert value(samples, "oncall_journal_up") == 0
    assert value(samples, "oncall_store_login_up") == 0
    assert names(samples) == {"oncall_journal_up", "oncall_journal_read_duration_seconds",
                              "oncall_store_login_up", "oncall_store_login_duration_seconds"}


def test_a_store_that_accepts_new_logins_reports_login_up(store_db):
    assert value(scrape(), "oncall_store_login_up") == 1


def test_a_failed_login_is_reported_while_pooled_reads_still_work(journalled, monkeypatch):
    """Drill D3, 2026-10-13: the store login was revoked, every pooled session kept
    working, journal_up stayed 1, and only new connections failed."""
    def revoked():
        raise RuntimeError('FATAL:  PAM authentication failed for user "oncall"')

    monkeypatch.setattr(exporter.connection, "fresh_login", revoked)
    samples = scrape()

    assert value(samples, "oncall_journal_up") == 1
    assert value(samples, "oncall_store_login_up") == 0


def test_the_login_probe_never_borrows_from_the_pool(store_db):
    """A probe that took a pooled session would answer with a login made long ago."""
    from oncall.store import connection

    connection.close()
    pid = connection.fresh_login()

    assert pid > 0
    assert connection._pool is None


def test_the_bucket_bounds_are_the_ones_documented():
    assert exporter.LATENCY_BUCKETS == (5, 10, 15, 20, 25, 30, 45, 60, 90, 120, 180, 300)
    assert list(exporter.LATENCY_BUCKETS) == sorted(exporter.LATENCY_BUCKETS)


def test_a_call_exactly_on_a_bucket_bound_is_inside_it(buffer, store_db):
    """le is inclusive in Prometheus' histogram model: a 30.000 s call belongs in le=30."""
    ship(record("x", Outcome.OK, latency_ms=30_000))
    samples = scrape()

    assert value(samples, "oncall_diagnosis_latency_seconds_bucket", model=MODEL, le="25.0") == 0
    assert value(samples, "oncall_diagnosis_latency_seconds_bucket", model=MODEL, le="30.0") == 1
