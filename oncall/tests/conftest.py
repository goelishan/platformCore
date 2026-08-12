"""
Shared fixtures.

The buffer needs nothing — it is a file, and every test gets its own. The store needs a
real Postgres, because the point of having one is the features SQLite does not have:
jsonb containment, partitioned tables, window functions. Testing those against a
stand-in would test the stand-in.

Store tests skip when no database is reachable, rather than fail. A skip says "not
verified here"; a failure says "broken", and a missing dev container is not a broken
codebase. The suite therefore stays runnable with nothing installed, which is what keeps
the pure-mapping tests as fast as they were.

    docker compose -f oncall/dev/compose.yaml up -d
"""

from __future__ import annotations

import pytest

from oncall import config
from oncall import landing_zone as lz


@pytest.fixture()
def buffer(tmp_path, monkeypatch):
    """A private buffer per test. config resolves paths at call time, so patching the
    module attributes is enough."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "BUFFER_DB_PATH", tmp_path / "buffer.db")
    monkeypatch.setattr(config, "BLOB_DIR", tmp_path / "blobs")
    lz.bootstrap()
    return tmp_path


@pytest.fixture(scope="session")
def store_available() -> bool:
    from oncall import store

    try:
        with store.connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 - any failure to reach it means the same thing
        return False
    finally:
        from oncall.store import connection

        connection.close()


@pytest.fixture()
def store_db(store_available, monkeypatch):
    """A migrated, empty store.

    Truncating rather than recreating the database: migrations are the thing under test
    in one case and an expensive fixed cost in every other. CASCADE reaches the
    partitions, which are separate tables and would otherwise survive a truncate of the
    parent.
    """
    if not store_available:
        pytest.skip("no store reachable; docker compose -f oncall/dev/compose.yaml up -d")

    from oncall import shipper, store
    from oncall.store import connection

    # Discard whatever pool exists before doing anything. The pool captures the DSN when
    # it is built and caches it, so a test that repointed config at a dead port leaves a
    # pool that keeps dialling that port long after monkeypatch has restored the config.
    # Rebuilding here rather than trusting the previous test to clean up is what makes
    # each case independent of the order it happens to run in.
    connection.close()

    store.migrate()

    # The shipper latches after its first successful migration. Left set between tests,
    # a case that expects migrations to run would silently not run them.
    monkeypatch.setattr(shipper, "_schema_ready", False)

    with store.connect() as conn:
        conn.execute("TRUNCATE signals, incidents, diagnoses CASCADE")

    yield store

    # Teardown runs after monkeypatch has undone its patches — function-scoped fixtures
    # tear down in reverse order of setup, and monkeypatch is requested last by the tests
    # that need it. So closing here rebuilds against the restored DSN, and the truncate
    # below reaches a live database rather than the dead port the test was using.
    connection.close()

    with store.connect() as conn:
        conn.execute("TRUNCATE signals, incidents, diagnoses CASCADE")
