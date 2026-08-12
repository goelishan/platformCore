"""
Schema migrations for the durable store.

  - The buffer bootstraps with CREATE TABLE IF NOT EXISTS because it is disposable:
    delete the file and it rebuilds. The store is not disposable, so its schema changes
    have to be ordered, recorded, and applied exactly once.
  - Hand-rolled rather than Alembic. There is no ORM here for autogenerate to
    introspect, so Alembic would contribute a dependency and a directory layout while
    the migrations themselves stayed hand-written SQL either way.
  - An advisory lock serialises runners. Two agent replicas starting together would
    otherwise race, and the loser fails on a duplicate object rather than waiting.
  - Each file applies inside its own transaction together with the row recording it.
    A migration that fails leaves no trace of having been attempted, so the retry is
    a clean retry.
"""

from __future__ import annotations

import logging
from pathlib import Path

from oncall.store import connection

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Any constant works as long as nothing else in the database picks the same one. Derived
# from the application name so it is greppable rather than magic.
_LOCK_KEY = 0x0C_A1_10_AD

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def available() -> list[Path]:
    """Sorted by filename, which is why they are numbered rather than named by date or
    by hash. Order is the entire contract of a migration directory."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def applied(conn) -> set[str]:
    conn.execute(_MIGRATIONS_TABLE)
    rows = connection.fetch_all(conn, "SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


def migrate() -> list[str]:
    """Apply everything outstanding. Returns the versions applied, empty when current.

    Safe to call on every start. That is deliberate: a deploy that forgets to run
    migrations is a class of outage worth designing out rather than documenting.
    """
    done: list[str] = []

    with connection.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        already = applied(conn)

        for path in available():
            version = path.stem
            if version in already:
                continue

            log.info("applying migration %s", version)
            conn.execute(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
            )
            done.append(version)

    return done


def pending() -> list[str]:
    """What migrate() would apply. Lets a readiness check fail loudly on a schema older
    than the code, instead of the code failing later on a missing column."""
    with connection.connect() as conn:
        already = applied(conn)
    return [p.stem for p in available() if p.stem not in already]
