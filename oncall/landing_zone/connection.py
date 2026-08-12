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

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from oncall import config

SCHEMA_PATH=Path(__file__).resolve().parent / "schema.sql"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    config.BLOB_DIR.mkdir(parents=True, exist_ok=True)

    conn=sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        _ensure_incremental_vacuum(conn)
    finally:
        conn.close()


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