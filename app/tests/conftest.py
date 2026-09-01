#--------------------------------------------------------------------------------------------------------
# SHARED FIXTURES — THE APP WITH NO DATABASE AND NO AWS
#--------------------------------------------------------------------------------------------------------
#
#   - path shim so `main` imports the way the container runs it
#
#   - environment holding the four variables get_db_connection insists on
#
#   - psycopg.connect spy, the real boundary a socket would be opened at
#
#   - readiness cache reset around every case
#


import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The Dockerfile copies app/ to the image root and runs `uvicorn main:app`, so the
# suite imports the module by the same name rather than making app/ a package, which
# it is not in production.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


REQUIRED_ENV = {
    "RDS_HOST": "db.invalid",
    "RDS_USER": "fastapi",
    "RDS_DB_NAME": "platformcore",
    "AWS_DEFAULT_REGION": "us-east-1",
}


class FakeCursor:
    """Answers the one query /ready asks. Records it, so a test can assert the
    readiness path actually reached the database rather than short-circuiting."""

    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)

    def fetchone(self):
        return (1,)


class FakeConnection:
    def __init__(self):
        self.cursors = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        cursor = FakeCursor()
        self.cursors.append(cursor)
        return cursor

    def close(self):
        pass


class ConnectSpy:
    """Stands in for psycopg.connect: counts calls and does what the test tells it."""

    def __init__(self):
        self.calls = []
        self.connections = []
        self.error = None

    def __call__(self, conn_str, **kwargs):
        self.calls.append(conn_str)
        if self.error is not None:
            raise self.error
        conn = FakeConnection()
        self.connections.append(conn)
        return conn

    def raising(self, exc):
        """Fail every subsequent attempt, the way an unreachable RDS does."""
        self.error = exc


@pytest.fixture(autouse=True)
def app_environment(monkeypatch):
    """A known environment and a cold readiness cache for every case.

    The cache is process-global by design, so without the reset a passing /ready would
    answer on behalf of the failure cases that run after it."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # The IAM token is signed locally from credentials that do not exist in CI, and is
    # not what any of these tests are about. The driver call it feeds is.
    monkeypatch.setattr(main, "get_rds_auth_token", lambda: "iam-token")
    main._reset_ready_cache()
    yield
    main._reset_ready_cache()


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture()
def connect_spy(monkeypatch):
    """Patch psycopg.connect itself rather than anything in main.

    get_db_connection imports psycopg inside the function body, so there is no module
    attribute to stub. psycopg.connect is the boundary that would open a socket, which
    makes it both the honest seam and the guarantee that the suite needs no database."""
    import psycopg

    spy = ConnectSpy()
    monkeypatch.setattr(psycopg, "connect", spy)
    return spy
