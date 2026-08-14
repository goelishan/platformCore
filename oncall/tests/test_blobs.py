"""
The blob path: content-addressed files beside the buffer.

The interesting assertions here are about the two orders — file before row on write,
row before file on delete — because both exist to make a crash leave the recoverable
kind of wreckage. Neither is observable from a happy-path round trip, so the tests
interrupt the sequence on purpose.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from oncall import landing_zone as lz
from oncall.envelope import Signal, SignalKind, SignalSource, Subject
from oncall.landing_zone import blobs

LOG = b"panic: runtime error: invalid memory address\ngoroutine 1 [running]:\n"


def _signal(blob_id: str | None = None, name: str = "web-1") -> Signal:
    return Signal(
        source=SignalSource.K8S_LOGS,
        kind=SignalKind.LOG_EXCERPT,
        cluster="oncall-dev",
        event_time=datetime(2026, 8, 14, 3, 14, 7, tzinfo=UTC),
        namespace="oncall-lab",
        subject=Subject(kind="Pod", name=name, uid=f"u-{name}"),
        dedupe_key=f"app|panic|{name}",
        blob_id=blob_id,
    )


# ---- identity and layout ---------------------------------------------------


def test_blob_id_is_the_digest_of_the_bytes(buffer):
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG)

    assert blob_id == hashlib.sha256(LOG).hexdigest()


def test_writing_the_same_bytes_twice_is_one_blob(buffer):
    """Content addressing is what makes a re-collected log cost nothing. A crashlooping
    container emits the same lines on every restart, and an id derived from anything
    else would store them once per poll."""
    with lz.connect() as conn:
        first = blobs.put_blob(conn, LOG)
        second = blobs.put_blob(conn, LOG)
        count = conn.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()["n"]

    assert first == second
    assert count == 1


def test_path_is_stored_relative_to_the_blob_directory(buffer):
    """An absolute path survives until the data directory moves between a laptop, a PVC
    and a restored backup. After that every row asserts something false, and the failure
    is one directory listing away from being invisible."""
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG)
        stored = conn.execute(
            "SELECT path FROM blobs WHERE blob_id = ?", (blob_id,)
        ).fetchone()["path"]

    assert not stored.startswith("/")
    assert stored.endswith(blob_id)
    assert (buffer / "blobs" / stored).exists()


def test_files_are_sharded_rather_than_piled_in_one_directory(buffer):
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG)

    expected = buffer / "blobs" / blob_id[:2] / blob_id[2:4] / blob_id
    assert expected.exists()


# ---- reading ---------------------------------------------------------------


def test_round_trip_returns_the_exact_bytes(buffer):
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG)
        result = blobs.read_blob(conn, blob_id)

    assert result == blobs.Blob(LOG, blobs.OK)


def test_unknown_blob_reads_as_missing_not_as_an_error(buffer):
    """Retention deletes blobs on purpose, so absence is the routine outcome. Raising
    would take a whole diagnosis down over an excerpt that expired as designed."""
    with lz.connect() as conn:
        assert blobs.read_blob(conn, "0" * 64).status == blobs.MISSING


def test_a_row_without_its_file_reads_as_gone_not_as_missing(buffer):
    """The distinction is the point. 'missing' is retention working; 'gone' means the
    write path or a sweep is broken, and collapsing both to None hides the bug behind
    the routine case forever."""
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG)
        rel = conn.execute(
            "SELECT path FROM blobs WHERE blob_id = ?", (blob_id,)
        ).fetchone()["path"]

    (buffer / "blobs" / rel).unlink()

    with lz.connect() as conn:
        assert blobs.read_blob(conn, blob_id).status == blobs.GONE


def test_contents_are_verified_against_the_id_on_every_read(buffer):
    """A corrupted excerpt reaching a prompt is indistinguishable from something the
    application logged. The name is a checksum; not checking it wastes the property."""
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG)
        rel = conn.execute(
            "SELECT path FROM blobs WHERE blob_id = ?", (blob_id,)
        ).fetchone()["path"]

    (buffer / "blobs" / rel).write_bytes(b"something else entirely")

    with lz.connect() as conn:
        result = blobs.read_blob(conn, blob_id)

    assert result.status == blobs.CORRUPT
    assert result.data is None


# ---- the two orders --------------------------------------------------------


def test_a_rolled_back_transaction_leaves_a_file_and_no_row(buffer):
    """The file is written before the row and outside the transaction, so this is the
    designed failure: an orphan, which a sweep reclaims. The alternative order leaves a
    row pointing at nothing, which no query can detect and no sweep can fix."""
    blob_id = hashlib.sha256(LOG).hexdigest()

    with pytest.raises(RuntimeError):
        with lz.connect() as conn:
            blobs.put_blob(conn, LOG)
            raise RuntimeError("collector died mid-run")

    with lz.connect() as conn:
        assert blobs.read_blob(conn, blob_id).status == blobs.MISSING

    assert (buffer / "blobs" / blobs.relative_path(blob_id)).exists()


def test_the_orphan_sweep_reclaims_it(buffer):
    blob_id = hashlib.sha256(LOG).hexdigest()

    with pytest.raises(RuntimeError):
        with lz.connect() as conn:
            blobs.put_blob(conn, LOG)
            raise RuntimeError("collector died mid-run")

    with lz.connect() as conn:
        removed = blobs.sweep_orphan_files(
            conn, now=datetime.now(UTC) + timedelta(hours=1)
        )

    assert removed == 1
    assert not (buffer / "blobs" / blobs.relative_path(blob_id)).exists()


def test_the_orphan_sweep_will_not_race_an_in_flight_write(buffer):
    """put_blob writes the file first, so every write in progress is momentarily an
    orphan by this definition. Without the grace window the sweep deletes blobs out from
    under the collector that is still committing them, and the resulting row reads as
    'gone' — a bug that only appears under concurrency."""
    with lz.connect() as conn:
        blobs.put_blob(conn, LOG)
        removed = blobs.sweep_orphan_files(conn, now=datetime.now(UTC))

    assert removed == 0


def test_no_temp_files_survive_a_completed_write(buffer):
    with lz.connect() as conn:
        blobs.put_blob(conn, LOG)

    leftovers = [p.name for p in (buffer / "blobs").rglob(".tmp-*")]
    assert leftovers == []


# ---- retention -------------------------------------------------------------


def test_re_collection_slides_expiry_without_moving_created_at(buffer):
    """Same argument as a re-collected signal: the problem is still being observed, so
    the data is still wanted. created_at stays put because the size backstop drops in
    that order, and a blob that keeps refreshing its own drop priority never drops."""
    first = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)
    later = first + timedelta(hours=6)

    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG, now=first)
        blobs.put_blob(conn, LOG, now=later)
        row = conn.execute(
            "SELECT created_at, expires_at FROM blobs WHERE blob_id = ?", (blob_id,)
        ).fetchone()

    assert row["created_at"].startswith("2026-08-14T03:00")
    assert row["expires_at"] > row["created_at"]
    assert not row["expires_at"].startswith("2026-08-16T03:00")


def test_the_age_sweep_will_not_delete_a_referenced_blob(buffer):
    """A signal outliving its blob is a dangling pointer. The age sweep is the routine
    path and is never allowed to create one; only the size backstop may, and only
    because a full volume is worse."""
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG, retention=timedelta(seconds=-1))
        lz.write_signals(conn, None, [_signal(blob_id=blob_id)])

    with lz.connect() as conn:
        result = blobs.collect_garbage(conn)
        status = blobs.read_blob(conn, blob_id).status

    assert result["aged_out"] == 0
    assert status == blobs.OK


def test_an_expired_unreferenced_blob_loses_both_row_and_file(buffer):
    with lz.connect() as conn:
        blob_id = blobs.put_blob(conn, LOG, retention=timedelta(seconds=-1))

    with lz.connect() as conn:
        result = blobs.collect_garbage(conn)

    assert result["aged_out"] == 1
    assert result["files_unlinked"] == 1
    assert not (buffer / "blobs" / blobs.relative_path(blob_id)).exists()


def test_the_size_backstop_gives_up_unreferenced_blobs_first(buffer, monkeypatch):
    """Cheapest loss before any real one. Chunk size is forced to 1 so the ordering is
    observable: at the default the whole batch leaves in a single statement and the
    priority never shows."""
    monkeypatch.setattr(blobs, "_DROP_CHUNK", 1)
    spare, wanted = b"x" * 100, b"y" * 100

    with lz.connect() as conn:
        loose = blobs.put_blob(conn, spare)
        kept = blobs.put_blob(conn, wanted)
        lz.write_signals(conn, None, [_signal(blob_id=kept)])

    with lz.connect() as conn:
        result = blobs.enforce_size(conn, max_bytes=150)
        assert blobs.read_blob(conn, loose).status == blobs.MISSING
        assert blobs.read_blob(conn, kept).status == blobs.OK

    assert result["dropped"] == 1
    assert result["dropped_referenced"] == 0


def test_dropping_a_blob_is_recorded_where_the_assembler_already_looks(buffer):
    """buffer_drops, not a table of its own. The assembler consults it before reading
    anything into a gap, and a second table is a second place to remember to look."""
    with lz.connect() as conn:
        blobs.put_blob(conn, b"z" * 200)

    with lz.connect() as conn:
        blobs.enforce_size(conn, max_bytes=1)
        row = conn.execute(
            "SELECT reason, rows_dropped FROM buffer_drops ORDER BY dropped_at DESC"
        ).fetchone()

    assert row["reason"] == "blob_size"
    assert row["rows_dropped"] == 1
