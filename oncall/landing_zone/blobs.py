"""
Raw payloads on disk, addressed by their content.

  - A signal row carries a trimmed excerpt; the blob carries what was actually read.
    Rows are queried, joined and shipped, so they have to stay small — a crashlooping
    container can emit megabytes between two polls, and putting that in a column makes
    every unrelated query pay for it.
  - blob_id is the sha256 of the bytes. Re-collecting an unchanged log therefore writes
    one blob rather than one per poll, and a retry after a crash is a no-op — the same
    property that makes signal writes idempotent, one layer down.
  - The file is written before the row, and the row is deleted before the file. Both
    orders leave the same kind of wreckage after a crash: a file with no row, which the
    orphan sweep reclaims. The opposite orientation leaves a row pointing at nothing,
    which is a promise the landing zone cannot keep and nothing can detect from SQL.
  - path is stored relative to BLOB_DIR. An absolute path is correct exactly once —
    until the data directory moves between a laptop, a PVC and a restored backup, at
    which point every row in the table is a lie about the local filesystem.
  - Blobs never ship. The store receives blob_id as a pointer that only resolves while
    the blob is still local, so a shipped signal can outlive the thing it points at.
    That is why read_blob() distinguishes 'missing' from 'gone' instead of returning None
    for both: one is retention working, the other is a bug.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from typing import Any, NamedTuple

from oncall import config
from oncall.envelope import iso
from oncall.landing_zone.retention import sweep_blobs
from oncall.landing_zone.writer import record_buffer_drop

log = logging.getLogger(__name__)

# Two levels of two hex characters. One flat directory holds every blob in the cluster
# and both `ls` and readdir() degrade badly past a few tens of thousands of entries; the
# fan-out keeps any single directory small without a nesting depth nobody can navigate.
_SHARD_DEPTH = 2
_SHARD_WIDTH = 2

_DROP_CHUNK = 200
_MAX_DROP_ITERATIONS = 1000


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative_path(blob_id: str) -> str:
    """Stored in the row and joined to BLOB_DIR on read. Derived from the id rather than
    recorded independently, so the two can never disagree — but still written to the
    column, because the layout is allowed to change and old rows must stay readable."""
    shards = [
        blob_id[i * _SHARD_WIDTH : (i + 1) * _SHARD_WIDTH] for i in range(_SHARD_DEPTH)
    ]
    return "/".join([*shards, blob_id])


def absolute_path(rel: str, root: Path | None = None) -> Path:
    return (root or config.BLOB_DIR) / rel


# ---- writing ---------------------------------------------------------------


def put_blob(
    conn: Connection,
    data: bytes,
    now: datetime | None = None,
    retention: timedelta | None = None,
    root: Path | None = None,
) -> str:
    """Write the bytes and record them. Returns the blob_id for Signal.blob_id.

    The file write happens outside the caller's transaction because a filesystem write
    cannot participate in one — there is no rollback for os.replace. The row insert does
    join the transaction, so a collector run that fails after this point leaves no row,
    and the file it already wrote becomes an orphan the sweep reclaims. That asymmetry
    is the whole reason the order is file-then-row and not the reverse.
    """
    moment = now or datetime.now(UTC)
    blob_id = _digest(data)
    rel = relative_path(blob_id)
    path = absolute_path(rel, root)

    # An existing file under a content-addressed name is complete by construction: the
    # only way bytes reach that name is the atomic replace below, so a partial write
    # never acquires it. Rewriting would cost IO to produce the same file.
    if not path.exists():
        _write_atomic(path, data)

    conn.execute(
        "INSERT INTO blobs (blob_id, path, bytes, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?) "
        # Re-collection slides the expiry forward while the problem is still being
        # observed, exactly as it does for a signal. created_at is left alone: it is
        # when these bytes were first seen, and the size backstop drops in that order.
        "ON CONFLICT(blob_id) DO UPDATE SET expires_at = excluded.expires_at",
        (
            blob_id,
            rel,
            len(data),
            iso(moment),
            iso(moment + (retention or config.BLOB_RETENTION)),
        ),
    )
    return blob_id


def _write_atomic(path: Path, data: bytes) -> None:
    """Temp file in the destination directory, then os.replace.

    Writing straight to the final name would let a crash mid-write leave a truncated
    file under a name that asserts the hash of the whole. Nothing downstream would
    question it — the name is the checksum, and checking it is exactly what a caller
    skips when the file looks present. The temp file must share a directory with the
    destination because replace is only atomic within one filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            # The rename is atomic with respect to other readers, not with respect to
            # power loss. Without the fsync the directory entry can reach disk ahead of
            # the contents, which is the truncated-file case arriving by another route.
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---- reading ---------------------------------------------------------------

OK = "ok"
MISSING = "missing"
GONE = "gone"
CORRUPT = "corrupt"


class Blob(NamedTuple):
    """Four outcomes, because collapsing them to bytes-or-None hides the two that are
    bugs behind the one that is routine.

      ok       bytes read and verified against the id
      missing  no row: swept by retention, or never written. Expected.
      gone     row present, file absent. The write path or the sweep is broken.
      corrupt  file present, contents do not hash to the id.
    """

    data: bytes | None
    status: str


def read_blob(conn: Connection, blob_id: str, root: Path | None = None) -> Blob:
    """Never raises. This is called while assembling evidence during an incident, and a
    blob is by design the most disposable thing in the landing zone — taking a diagnosis
    down because an excerpt aged out would trade the whole answer for a footnote."""
    row = conn.execute(
        "SELECT path FROM blobs WHERE blob_id = ?", (blob_id,)
    ).fetchone()

    if row is None:
        return Blob(None, MISSING)

    path = absolute_path(row["path"], root)
    try:
        data = path.read_bytes()
    except OSError:
        log.warning("blob %s has a row but no readable file at %s", blob_id, path)
        return Blob(None, GONE)

    # Verified on every read rather than trusted. Reads are rare — once per diagnosis,
    # not once per poll — and the alternative is feeding a corrupted excerpt into a
    # prompt, where it is indistinguishable from something the application logged.
    if _digest(data) != blob_id:
        log.error("blob %s does not match its own digest", blob_id)
        return Blob(None, CORRUPT)

    return Blob(data, OK)


# ---- reclaiming ------------------------------------------------------------
# Rows are swept in retention.py, which is pure SQL. Everything that touches the
# filesystem lives here, so there is exactly one module where the table and the disk can
# be observed to disagree.


def unlink(paths: Iterable[str], root: Path | None = None) -> int:
    """Files whose rows are already gone. A failure to remove one is logged and stepped
    over: it leaves a file the orphan sweep will find again next cycle, whereas raising
    would abandon the rest of the batch for the sake of one unremovable path."""
    removed = 0
    for rel in paths:
        try:
            absolute_path(rel, root).unlink(missing_ok=True)
            removed += 1
        except OSError:
            log.warning("could not unlink blob file %s", rel, exc_info=True)
    return removed


def _stored_bytes(conn: Connection) -> int:
    """Summed from the table, not from the filesystem. The table is the record, a walk
    of the directory costs a stat per blob, and any difference between the two is by
    definition an orphan — which is a separate sweep with a separate answer."""
    row = conn.execute("SELECT COALESCE(SUM(bytes), 0) AS total FROM blobs").fetchone()
    return int(row["total"]) if row else 0


def sweep_orphan_files(
    conn: Connection,
    now: datetime | None = None,
    grace: timedelta | None = None,
    root: Path | None = None,
) -> int:
    """Files on disk with no row: the wreckage of a run that died between the write and
    the insert, or of a rolled-back transaction.

    The grace period is not politeness, it is correctness. put_blob() writes the file first,
    so every in-flight write is momentarily an orphan by this definition, and a sweep
    without a grace window would delete blobs out from under the collector that is
    still committing them.
    """
    directory = root or config.BLOB_DIR
    if not directory.exists():
        return 0

    cutoff = ((now or datetime.now(UTC)) - (grace or config.BLOB_ORPHAN_GRACE)).timestamp()
    known = {r["blob_id"] for r in conn.execute("SELECT blob_id FROM blobs")}

    removed = 0
    for path in directory.rglob("*"):
        if not path.is_file() or path.name in known:
            continue
        # Temp files from an interrupted _write_atomic are swept by the same rule; they
        # can never be in `known`, because the name only exists after the replace.
        if path.stat().st_mtime >= cutoff:
            continue

        path.unlink(missing_ok=True)
        removed += 1

    return removed


def enforce_size(
    conn: Connection,
    max_bytes: int | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """The backstop, and the only sweep permitted to delete a referenced blob.

    The age sweep in retention.py refuses to touch a blob any signal still points at, so
    under normal operation a pointer always resolves. This one may break that, for the
    same reason the buffer's size bound may drop unshipped rows: the agent shares a
    volume with the thing it is diagnosing, and a full volume takes the agent down
    during the incident it exists to explain. A dangling blob_id degrades one excerpt
    and reads as 'gone'; a full disk ends the diagnosis.

    Unreferenced blobs go first, so the cheap loss is always taken before the real one.
    """
    ceiling = max_bytes if max_bytes is not None else config.BLOB_MAX_BYTES
    result: dict[str, Any] = {"dropped": 0, "dropped_referenced": 0, "bytes": 0}

    for _ in range(_MAX_DROP_ITERATIONS):
        if _stored_bytes(conn) <= ceiling:
            break

        batch = conn.execute(
            "SELECT blob_id, path, bytes, created_at, "
            "  EXISTS (SELECT 1 FROM signals s WHERE s.blob_id = b.blob_id) AS referenced "
            "FROM blobs b ORDER BY referenced, created_at LIMIT ?",
            (_DROP_CHUNK,),
        ).fetchall()

        if not batch:
            break

        ids = [r["blob_id"] for r in batch]
        referenced = sum(1 for r in batch if r["referenced"])
        placeholders = ", ".join("?" for _ in ids)

        # Row first, file second: a crash here leaves an orphan file, which is
        # recoverable, rather than a row pointing at nothing, which is not.
        conn.execute(f"DELETE FROM blobs WHERE blob_id IN ({placeholders})", ids)
        unlink((r["path"] for r in batch), root)

        result["dropped"] += len(ids)
        result["dropped_referenced"] += referenced
        result["bytes"] += sum(int(r["bytes"] or 0) for r in batch)

        # Recorded in buffer_drops like every other thing the buffer gives up. The
        # assembler already consults that table before reading anything into a gap, and
        # a second table would mean a second place to remember to look.
        record_buffer_drop(
            conn,
            "blob_size",
            len(ids),
            referenced,
            min(r["created_at"] for r in batch),
            max(r["created_at"] for r in batch),
        )

    return result


def collect_garbage(
    conn: Connection,
    now: datetime | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """The whole reclaim cycle, in the order that makes each step cheaper than the last.

    Age first, because it is free and unreferenced. Orphans second, since the age sweep
    has just produced more of them. The size backstop last, so it only ever runs against
    what genuinely could not be released any other way.
    """
    aged = sweep_blobs(conn, now)
    unlinked = unlink(aged["orphan_paths"], root)
    orphans = sweep_orphan_files(conn, now, root=root)
    sized = enforce_size(conn, root=root)

    return {
        "aged_out": aged["blobs"],
        "files_unlinked": unlinked,
        "orphan_files": orphans,
        **{f"size_{k}": v for k, v in sized.items()},
    }
