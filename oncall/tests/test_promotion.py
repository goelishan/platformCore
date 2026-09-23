"""
Promotion tests: which log excerpts get their full text back, and what the bundle
says about the ones that do not.

  - Real blobs through the real put_blob, because the outcomes under test (gone,
    corrupt, over budget) are facts about the disk and the blobs table, and a stub
    of read_blob would only test the stub.
  - Order is the point. The budget is spent in the order of what each excerpt
    explains, so the tests pin that a higher-ranked failure's log wins the budget
    even when its excerpt is older.
  - Every blob that was considered is on the record, promoted or not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope import (
    Severity,
    Signal,
    SignalKind,
    SignalSource,
    SourceStatus,
    Subject,
    partition_payload,
)
from oncall.evidence import bundle
from oncall.landing_zone import blobs

CLUSTER = "test-cluster"
POD = "api-7f9"

NOW = datetime.now(UTC).replace(microsecond=0)
START = NOW - timedelta(minutes=30)
END = NOW + timedelta(minutes=1)


# ---- helpers ---------------------------------------------------------------


def failure(key: str, severity: Severity, minutes_ago: int = 10) -> Signal:
    return Signal(
        source=SignalSource.K8S_PODS,
        kind=SignalKind.POD_STATE,
        cluster=CLUSTER,
        event_time=NOW - timedelta(minutes=minutes_ago),
        namespace="prod",
        subject=Subject(kind="Pod", name=POD, uid="uid-1"),
        severity=severity,
        dedupe_key=key,
        payload={"reason": key},
    )


def excerpt(explains: Signal, blob_id: str | None, key: str, minutes_ago: int = 5) -> Signal:
    """A log excerpt as k8s_logs writes it: info severity, pointer in provenance."""
    return Signal(
        source=SignalSource.K8S_LOGS,
        kind=SignalKind.LOG_EXCERPT,
        cluster=CLUSTER,
        event_time=NOW - timedelta(minutes=minutes_ago),
        namespace="prod",
        subject=Subject(kind="Pod", name=POD, uid="uid-1"),
        severity=Severity.INFO,
        dedupe_key=key,
        blob_id=blob_id,
        payload=partition_payload(
            {"log_status": "ok", "trigger_fingerprint": explains.fingerprint},
            frozenset({"trigger_fingerprint"}),
        ),
    )


def put(data: bytes) -> str:
    with lz.connect() as conn:
        return lz.put_blob(conn, data)


def store(*signals: Signal) -> None:
    by_source: dict[SignalSource, list[Signal]] = {}
    for s in signals:
        by_source.setdefault(s.source, []).append(s)
    with lz.connect() as conn:
        for source, batch in by_source.items():
            run = lz.start_run(conn, source, CLUSTER)
            lz.write_signals(conn, run, batch)
            lz.finish_run(conn, run, SourceStatus.OK, len(batch))


def build() -> bundle.Bundle:
    with lz.connect() as conn:
        return bundle.build(conn, CLUSTER, START, END, subject_name=POD, history=False)


def by_reason(b: bundle.Bundle) -> dict[str, bundle.Promotion]:
    """Promotions keyed by the reason of the failure their excerpt explains."""
    reasons = {f.fingerprint: f.facts.get("reason") for f in b.findings}
    names = {f.fingerprint: reasons.get(f.explains or "") for f in b.findings}
    return {str(names[p.fingerprint]): p for p in b.promoted}


# ---- the basic path ---------------------------------------------------------


def test_a_log_finding_carries_its_blob_id(buffer):
    crash = failure("OOMKilled", Severity.ERROR)
    blob_id = put(b"java.lang.OutOfMemoryError\n")
    store(crash, excerpt(crash, blob_id, "log"))

    (log,) = [f for f in build().findings if f.source == SignalSource.K8S_LOGS]

    assert log.blob_ids == [blob_id]


def test_the_full_text_is_promoted_when_it_fits(buffer):
    crash = failure("OOMKilled", Severity.ERROR)
    text = b"starting\njava.lang.OutOfMemoryError: heap\n"
    store(crash, excerpt(crash, put(text), "log"))

    (p,) = build().promoted

    assert p.status == blobs.OK
    assert p.text == text.decode()
    assert p.bytes == len(text)


def test_only_the_newest_blob_of_a_finding_is_promoted(buffer):
    # Two polls of the same excerpt, each with its own text. The older blob covers
    # an earlier window; the newer one is what the excerpt in facts was cut from.
    crash = failure("OOMKilled", Severity.ERROR)
    store(
        crash,
        excerpt(crash, put(b"older window\n"), "log", minutes_ago=8),
        excerpt(crash, put(b"newer window\n"), "log", minutes_ago=2),
    )

    (p,) = build().promoted

    assert p.text == "newer window\n"


def test_a_finding_without_a_blob_is_not_considered(buffer):
    store(failure("OOMKilled", Severity.ERROR))

    assert build().promoted == []


# ---- the budget, and who gets it ------------------------------------------


def test_the_budget_goes_to_the_log_of_the_higher_ranked_failure(buffer, monkeypatch):
    # Each blob fits on its own; both together do not. The critical failure's
    # excerpt is older, so recency alone would hand the budget to the other one.
    monkeypatch.setattr(config, "BUNDLE_PROMOTE_BYTES", 150)
    critical = failure("NodeLost", Severity.CRITICAL)
    warning = failure("Slow", Severity.WARNING)
    store(
        critical,
        warning,
        excerpt(critical, put(b"c" * 100), "log-c", minutes_ago=8),
        excerpt(warning, put(b"w" * 100), "log-w", minutes_ago=2),
    )

    promoted = by_reason(build())

    assert promoted["NodeLost"].text == "c" * 100
    assert promoted["Slow"].text is None
    assert promoted["Slow"].reason.startswith("over budget")


def test_a_blob_that_does_not_fit_is_recorded_with_its_size(buffer, monkeypatch):
    monkeypatch.setattr(config, "BUNDLE_PROMOTE_BYTES", 10)
    crash = failure("OOMKilled", Severity.ERROR)
    store(crash, excerpt(crash, put(b"x" * 50), "log"))

    (p,) = build().promoted

    assert p.text is None
    assert p.bytes == 50


# ---- blobs that are not there -----------------------------------------------


def blob_path(blob_id: str):
    with lz.connect() as conn:
        (rel,) = conn.execute(
            "SELECT path FROM blobs WHERE blob_id = ?", (blob_id,)
        ).fetchone()
    return blobs.absolute_path(rel)


def test_a_blob_whose_file_is_gone_is_recorded_not_raised(buffer):
    # The foreign key keeps the blobs row alive while a signal points at it, so the
    # realistic loss is the file: a disk cleanup, a restored volume, a manual rm.
    crash = failure("OOMKilled", Severity.ERROR)
    blob_id = put(b"gone soon\n")
    store(crash, excerpt(crash, blob_id, "log"))
    blob_path(blob_id).unlink()

    (p,) = build().promoted

    assert p.status == blobs.GONE
    assert p.text is None


def test_a_blob_that_fails_its_digest_is_never_promoted(buffer):
    crash = failure("OOMKilled", Severity.ERROR)
    blob_id = put(b"the real bytes\n")
    store(crash, excerpt(crash, blob_id, "log"))
    blob_path(blob_id).write_bytes(b"tampered\n")

    (p,) = build().promoted

    assert p.status == blobs.CORRUPT
    assert p.text is None


# ---- what the receipt and the reader get -----------------------------------


def test_what_was_promoted_is_part_of_the_evidence_receipt(buffer):
    crash = failure("OOMKilled", Severity.ERROR)
    store(crash, excerpt(crash, put(b"text\n"), "log"))
    b = build()
    before = bundle.evidence_receipt(b)

    b.promoted = []

    assert bundle.evidence_receipt(b) != before


def test_the_rendered_view_shows_the_promoted_text(buffer):
    crash = failure("OOMKilled", Severity.ERROR)
    store(crash, excerpt(crash, put(b"java.lang.OutOfMemoryError\n"), "log"))

    assert "| java.lang.OutOfMemoryError" in bundle.render(build())
