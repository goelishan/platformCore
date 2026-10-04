"""
Credentials, connections and the boundary with the cluster.

  - The source scan fails the suite if a working credential or a weak TLS default is
    ever written into code again. Dev credentials live in oncall/.env and nowhere else.
  - The production guard is tested by its verdicts, not by importing in prod: the
    module raises at import, and a test that imported it that way could not recover.
  - Nothing here opens a real connection. The IAM path is exercised by replacing the
    two calls that would leave the process.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
from kubernetes.config import ConfigException

from oncall import config
from oncall import landing_zone as lz
from oncall.envelope import SignalSource, SourceStatus

PACKAGE = Path(config.__file__).resolve().parent

# Literal credentials and TLS downgrades as they would appear in code.
FORBIDDEN = {
    "literal password": re.compile(r"""password\s*=\s*["'][^"'<{]"""),
    "defaulted password variable": re.compile(r"""getenv\(\s*["']ONCALL_STORE_PASSWORD["']\s*,"""),
    "sslmode downgrade": re.compile(r"sslmode=(?:prefer|disable|allow)\b"),
    "sslmode downgrade default": re.compile(
        r"""getenv\(\s*["']ONCALL_STORE_SSLMODE["']\s*,\s*["'](?:prefer|disable|allow)["']"""
    ),
    # AKIAIOSFODNN7EXAMPLE is AWS's own documentation key, planted by the dry run so it
    # can prove the value never reaches a bundle. Allowed by name, and nothing else.
    "aws access key": re.compile(r"\b(?!AKIAIOSFODNN7EXAMPLE\b)(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def _source_files() -> list[Path]:
    return [
        p for p in PACKAGE.rglob("*.py")
        if "tests" not in p.parts and ".venv" not in p.parts and "scratch" not in p.parts
    ]


# ---- nothing secret in code --------------------------------------------------


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_no_credential_or_tls_downgrade_is_written_into_code(name):
    pattern = FORBIDDEN[name]
    hits = [
        f"{p.relative_to(PACKAGE)}:{n}"
        for p in _source_files()
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line)
    ]

    assert hits == [], f"{name} found in {hits}"


def test_the_dsn_never_carries_a_password():
    """The password travels beside the DSN, so no log line or repr of it can leak one."""
    assert "password" not in config.STORE_DSN


def test_the_dev_store_is_published_on_loopback_only():
    compose = (PACKAGE / "dev" / "compose.yaml").read_text()

    assert '"127.0.0.1:5433:5432"' in compose
    assert '- "5433:5432"' not in compose


def test_the_dev_compose_file_holds_no_password():
    """The dev password lives in oncall/.env only; compose interpolates it."""
    compose = (PACKAGE / "dev" / "compose.yaml").read_text()

    assert "POSTGRES_PASSWORD: ${ONCALL_STORE_PASSWORD:?" in compose


# ---- the env file ------------------------------------------------------------


def test_the_env_file_reads_only_oncall_keys_and_never_overrides(tmp_path, monkeypatch):
    """AWS credentials must never arrive through a file this package reads, and the
    real environment always wins over the file."""
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "ONCALL_TEST_FROM_FILE=file\n"
        "ONCALL_TEST_ALREADY_SET=file\n"
        "AWS_SECRET_ACCESS_KEY=should-not-load\n"
    )
    monkeypatch.delenv("ONCALL_TEST_FROM_FILE", raising=False)
    monkeypatch.setenv("ONCALL_TEST_ALREADY_SET", "real")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    config._load_env_file(env)

    import os

    assert os.environ["ONCALL_TEST_FROM_FILE"] == "file"
    assert os.environ["ONCALL_TEST_ALREADY_SET"] == "real"
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ
    monkeypatch.delenv("ONCALL_TEST_FROM_FILE", raising=False)


# ---- the production guard ----------------------------------------------------


def _prod(monkeypatch, **overrides):
    """A production configuration that passes, with any field overridden."""
    values = {
        "STORE_SSLMODE": "verify-full",
        "STORE_SSLROOTCERT": "/etc/ssl/rds-global-bundle.pem",
        "STORE_IAM_AUTH": True,
        "STORE_PASSWORD": None,
        "AWS_REGION": "us-east-1",
        "STORE_DSN": "host=db port=5432 dbname=oncall user=oncall sslmode=verify-full",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setenv("ONCALL_STORE_HOST", "db.example.internal")
    monkeypatch.delenv("ONCALL_STORE_DSN", raising=False)


def test_a_sound_production_configuration_passes(monkeypatch):
    _prod(monkeypatch)

    assert config._production_problems() == []


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"STORE_SSLMODE": "prefer"}, "must be verify-full"),
        ({"STORE_SSLROOTCERT": None}, "SSLROOTCERT"),
        ({"STORE_IAM_AUTH": False}, "IAM_AUTH must be true"),
        ({"STORE_PASSWORD": "static"}, "PASSWORD must not be set"),
        ({"AWS_REGION": None}, "AWS_REGION"),
    ],
)
def test_production_refuses_what_is_only_safe_in_dev(monkeypatch, override, fragment):
    _prod(monkeypatch, **override)

    assert any(fragment in p for p in config._production_problems())


def test_production_refuses_an_implicit_store_host(monkeypatch):
    _prod(monkeypatch)
    monkeypatch.delenv("ONCALL_STORE_HOST")

    assert any("STORE_HOST" in p for p in config._production_problems())


# ---- RDS IAM auth ------------------------------------------------------------


def test_every_connection_gets_a_fresh_iam_token(monkeypatch):
    """A pool that captured one token at startup authenticated for fifteen minutes and
    then failed for the rest of the process's life."""
    import psycopg

    from oncall.store import connection

    tokens = iter(["token-1", "token-2"])
    seen: list[str] = []

    monkeypatch.setattr(connection, "_iam_token", lambda: next(tokens))
    monkeypatch.setattr(
        psycopg.Connection, "connect",
        classmethod(lambda cls, conninfo="", **kw: seen.append(kw["password"])),
    )

    connection._IamAuthConnection.connect("host=db")
    connection._IamAuthConnection.connect("host=db")

    assert seen == ["token-1", "token-2"]


def test_a_static_password_is_passed_beside_the_dsn_and_never_with_iam(monkeypatch):
    from oncall.store import connection

    monkeypatch.setattr(config, "STORE_PASSWORD", "dev-only")
    monkeypatch.setattr(config, "STORE_IAM_AUTH", False)
    assert connection._pool_kwargs()["password"] == "dev-only"

    monkeypatch.setattr(config, "STORE_IAM_AUTH", True)
    assert "password" not in connection._pool_kwargs()


# ---- the cluster boundary ----------------------------------------------------


def test_without_a_named_context_the_client_refuses_to_guess(monkeypatch):
    """A collector that used whichever context kubectl last switched to could read a
    production cluster from a laptop."""
    from oncall.collectors import k8s_client

    def not_in_cluster():
        raise ConfigException("not in a cluster")

    monkeypatch.setattr(k8s_client.kube_config, "load_incluster_config", not_in_cluster)
    monkeypatch.setattr(config, "KUBE_CONTEXT", None)

    with pytest.raises(ConfigException, match="ONCALL_KUBE_CONTEXT"):
        k8s_client.load_auth()


def test_the_named_context_is_the_one_loaded(monkeypatch):
    from oncall.collectors import k8s_client

    def not_in_cluster():
        raise ConfigException("not in a cluster")

    loaded: list[str | None] = []
    monkeypatch.setattr(k8s_client.kube_config, "load_incluster_config", not_in_cluster)
    monkeypatch.setattr(
        k8s_client.kube_config, "load_kube_config", lambda context=None: loaded.append(context)
    )
    monkeypatch.setattr(config, "KUBE_CONTEXT", "kind-oncall")

    k8s_client.load_auth()

    assert loaded == ["kind-oncall"]


def test_the_collectors_never_call_a_write_verb():
    """Diagnose-only is enforced by the read-only ClusterRole in the cluster. This is
    the same promise at the code level, so a write never reaches review as a surprise."""
    verbs = re.compile(r"\.(?:create|patch|replace|delete)_[a-z_]+\(")
    hits = [
        f"{p.name}:{n}"
        for p in (PACKAGE / "collectors").glob("*.py")
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if verbs.search(line)
    ]

    assert hits == []


# ---- stored error text -------------------------------------------------------


def test_run_errors_are_redacted_and_bounded_before_they_are_stored(buffer):
    """Exceptions quote what they were handed, and the error column is rendered to the
    model through the source block."""
    error = (
        "OperationalError: connection to postgres://oncall:hunter2trombone@db failed "
        + "x" * 900
    )

    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.K8S_PODS, "test-cluster")
        lz.finish_run(conn, run, SourceStatus.UNAVAILABLE, 0, error)
        stored = conn.execute(
            "SELECT error FROM collection_runs WHERE run_id = ?", (run,)
        ).fetchone()["error"]

    assert "hunter2trombone" not in stored
    assert len(stored) <= 500


def test_the_owner_only_mode_constants_are_what_they_claim():
    from oncall.landing_zone import connection

    assert stat.S_IMODE(connection.DIR_MODE) == 0o700
    assert stat.S_IMODE(connection.FILE_MODE) == 0o600
