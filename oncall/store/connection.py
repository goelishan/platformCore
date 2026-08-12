"""
Connections to the durable store.

  - A pool, because the alternative is a connect per call and a TCP handshake plus
    authentication on every write. It is small on purpose: one shipper, one UI, one
    background collector. A large pool would only hide a leak, and every idle
    connection costs the server a backend process.
  - Created lazily. Importing this module must not open a socket — collectors import
    the package transitively and must keep working when the store is down.
  - Every failure is raised, never swallowed. The caller decides what an unreachable
    store means, and for the shipper it means the batch stays in the outbox.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from oncall import config

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """One pool per process, opened on first use.

    open=False then open() rather than letting the constructor connect, so that
    building the pool cannot block on a store that is down; the wait happens where a
    caller can time it out.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=config.STORE_DSN,
            min_size=config.STORE_POOL_MIN,
            max_size=config.STORE_POOL_MAX,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": config.STORE_CONNECT_TIMEOUT,
            },
            open=False,
        )
        _pool.open()
    return _pool


@contextmanager
def connect() -> Iterator[Connection]:
    """A pooled connection wrapped in a transaction.

    Commit on success, roll back on any exception. A partially shipped batch would
    leave the store holding some of a snapshot, and nothing downstream could tell that
    apart from a complete one.
    """
    with pool().connection(timeout=config.STORE_CONNECT_TIMEOUT) as conn, conn.transaction():
        yield conn


def close() -> None:
    """Shut the pool down. Tests use it to avoid leaking backends between cases; a
    long-running process never needs it."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def reachable() -> bool:
    """A cheap liveness probe that answers rather than raises.

    Used where an unreachable store is an expected condition to report — a UI banner,
    a readiness endpoint — not on the shipping path, which needs the actual error text
    to write into its run record.
    """
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001 - the whole point is to not care why
        log.debug("store unreachable: %s", exc)
        return False


def fetch_all(conn: Connection, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    """psycopg is cursor-based where sqlite3 is connection-based. Rather than sprinkle
    cursor handling through every query, the two helpers here restore the shape the
    buffer's reader already uses."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: Connection, sql: str, params: Any = ()) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
