"""
Secrets removed before the first write.

Every test asserts the secret is absent from the output, not merely that the call
returned or that the text changed. A rule that fires and leaves the value behind is
the only failure mode that matters here, and it is invisible to a test that checks
the flag alone.
"""

from __future__ import annotations

import pytest

from oncall.envelope import redact
from oncall.envelope.redact import rules_fired

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)

SECRETS = [
    ("bearer", "Authorization failed for Bearer sk-live-9f2b7c1d4e8a", "sk-live-9f2b7c1d4e8a"),
    ("jwt", f"token rejected: {JWT}", JWT),
    ("assignment", "starting with password=hunter2trombone", "hunter2trombone"),
    ("assignment", 'config: api_key="AKI-not-a-real-value"', "AKI-not-a-real-value"),
    ("dsn_credentials", "dial postgres://admin:s3cr3tpw@db:5432/oncall", "s3cr3tpw"),
    ("aws_access_key", "using AKIAIOSFODNN7EXAMPLE for s3", "AKIAIOSFODNN7EXAMPLE"),
    (
        "private_key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA",
    ),
]


@pytest.mark.parametrize(("name", "line", "secret"), SECRETS)
def test_the_secret_is_gone_from_the_output(name, line, secret):
    cleaned, was_redacted = redact(line)

    assert secret not in cleaned
    assert was_redacted is True


@pytest.mark.parametrize(("name", "line", "secret"), SECRETS)
def test_the_rule_that_fired_is_nameable(name, line, secret):
    assert name in rules_fired(line)


def test_ordinary_text_is_returned_unchanged():
    """Identity, not just "no crash". A redactor that quietly rewrites clean lines
    corrupts every excerpt it touches, and the damage is only visible by comparison
    with a source nobody keeps."""
    line = "Back-off restarting failed container app in pod web-1"
    cleaned, was_redacted = redact(line)

    assert cleaned == line
    assert was_redacted is False


def test_the_flag_distinguishes_our_marker_from_the_application_s_own_text():
    """An app that logs the literal string <redacted> must not make the signal claim
    something was removed, or the assembler cannot trust the flag at all."""
    cleaned, was_redacted = redact("upstream returned <redacted> for the field")

    assert was_redacted is False
    assert cleaned == "upstream returned <redacted> for the field"


def test_key_name_survives_so_the_reader_knows_what_was_removed():
    """Knowing a password was present is diagnostic. Which password it was is not, and
    is the thing that must not be written down."""
    cleaned, _ = redact("db_password=hunter2trombone")

    assert "db_password" in cleaned
    assert "hunter2trombone" not in cleaned


def test_empty_input_is_not_reported_as_redacted():
    assert redact(None) == ("", False)
    assert redact("") == ("", False)
