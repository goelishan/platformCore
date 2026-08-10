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

    path = db_path or config.DB_PATH
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

    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    config.BLOB_DIR.mkdir(parents=True, exist_ok=True)

    conn=sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()