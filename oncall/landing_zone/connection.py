"""
Connection lifecycle for the landing zone.

  - foreign_keys is per-connection and defaults OFF, so it is applied on every
    connect. Setting it in schema.sql would only cover the bootstrap connection,
    leaving every later connection with FK enforcement silently disabled.
  - busy_timeout because WAL permits exactly one writer; without it a second
    writer fails instantly instead of waiting out transient contention.
  - No module-level connection. Streamlit re-runs its script on every interaction
    and sqlite3 objects are not safe to share across threads.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from oncall import config

SCHEMA_PATH=Path(__file__).resolve().parent / "schema.sql"

# Owner-only for everything the agent keeps on disk. The buffer holds redacted rows but
# the blob directory holds raw container output, and a default umask leaves both
# readable by any other user on the host.
DIR_MODE = 0o700
FILE_MODE = 0o600


def _private_dir(path: Path) -> None:
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    # mkdir's mode applies only on creation; an existing directory keeps what it had.
    os.chmod(path, DIR_MODE)


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:

    path = db_path or config.BUFFER_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bootstrap(db_path: Path | None = None) -> None:

    path = db_path or config.BUFFER_DB_PATH
    _private_dir(path.parent)
    _private_dir(config.BLOB_DIR)

    conn=sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        _ensure_added_columns(conn)
        conn.commit()
        _ensure_incremental_vacuum(conn)
    finally:
        conn.close()
    # SQLite creates the file with the process umask, and the -wal and -shm sidecars
    # copy the database file's mode, so setting it here covers all three.
    os.chmod(path, FILE_MODE)


# Columns added to schema.sql after a buffer may already exist on disk.
#
# The schema is CREATE TABLE IF NOT EXISTS, which is what makes bootstrap idempotent
# and also what makes it silent: an existing file keeps whatever shape it was created
# with, and the first write naming a new column fails at runtime rather than at
# startup. The store has a migration runner; the buffer deliberately does not, because
# it is rebuildable and a two-day cache does not deserve one.
#
# Rebuildable is not the same as disposable, though. A buffer holding unshipped rows
# is the only copy of them, so "delete it and start again" costs real evidence at
# exactly the moment the store is down and the outbox is deepest. Adding a column is
# the one schema change that cannot lose data, so that subset is reconciled here and
# nothing else is: anything beyond it — a changed type, a dropped column, a new
# constraint — is a rebuild, and should be an explicit one rather than something this
# function attempts quietly.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "diagnoses": {"prompt_sha256": "TEXT"},
}


def _ensure_added_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            # The table is not there at all, which means schema.sql just created it
            # with every column, or this is not a buffer. Either way, nothing to add.
            continue

        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# 2 is INCREMENTAL; 0 is none. SQLite accepts "PRAGMA auto_vacuum = INCREMENTAL" and
# silently ignores it once the file header exists, so the only safe move is to read the
# value back rather than trust that setting it worked.
_INCREMENTAL = 2


def _ensure_incremental_vacuum(conn: sqlite3.Connection) -> None:
    """Bring a buffer without incremental vacuum into line.

    Two databases reach this: one created before the pragma was ordered correctly in
    schema.sql, and one created by a future edit that reorders it back. Both look
    healthy and both grow forever, because deleted pages go to a freelist that nothing
    ever reclaims — the failure appears as a volume filling up weeks later.

    VACUUM is the only way to change the setting on an existing file, and it is exactly
    the blocking full rewrite incremental mode exists to avoid. Running it here is
    acceptable because it happens once, at startup, before any collector is writing.
    """
    if conn.execute("PRAGMA auto_vacuum").fetchone()[0] == _INCREMENTAL:
        return

    conn.execute(f"PRAGMA auto_vacuum = {_INCREMENTAL}")
    # VACUUM cannot run inside a transaction, and sqlite3 opens one implicitly for DML.
    conn.isolation_level = None
    conn.execute("VACUUM")
    conn.isolation_level = ""