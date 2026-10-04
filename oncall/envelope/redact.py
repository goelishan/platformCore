"""
Secrets removed before anything is stored.

  - Runs on the write path, not before the prompt. The buffer is a file on a volume
    and the store is Postgres heading for RDS, so "we strip it before the LLM sees
    it" still means the credential was written to disk twice and backed up from
    there. The only place a secret is definitely not persisted is upstream of the
    first write.
  - The bias is the opposite of template.py, and deliberately so. There, over-merging
    is the unrecoverable error, so doubt keeps the token. Here, over-redaction costs
    a line of evidence the operator can still read with kubectl — the tool is
    diagnose-only and there is a human at the keyboard — while under-redaction writes
    a live credential into a database, a backup, and a third-party prompt. Doubt
    removes the token.
  - The signal records that redaction happened, never what was removed. A payload
    field naming the matched rule is the most a row may carry; the value is gone.
  - Pass whole blocks, never one line at a time. A private key spans lines, and the
    rule that recognises it needs BEGIN and END in one string: applied per line it can
    never fire, and the key body passes through as ordinary base64. That was the
    behaviour of the log excerpt until 2026-09-30.
"""

from __future__ import annotations

from oncall.envelope.masking import Rule, apply, rule

# Ordered by specificity. The structured forms come first so a key=value secret is
# recognised as one rather than being partly consumed by the generic high-entropy
# rule, which would leave the key name attached to a fragment of its own value.

# Every rule is idempotent: it never matches its own placeholder. The finished bundle
# is redacted a second time as a last defence, and a rule that fired on "<redacted>"
# would report a leak in every bundle that had ever caught one.
RULES: tuple[Rule, ...] = (
    # PEM blocks are matched whole. Line by line, the body is indistinguishable from
    # any other base64 and the guard lines would survive to say a key was here.
    rule(
        "private_key",
        r"(?s)-----BEGIN[^-]{0,40}PRIVATE KEY-----.*?"
        r"-----END[^-]{0,40}PRIVATE KEY-----",
        "<private-key>",
    ),
    # A key whose END was cut off, by a byte cap or a crash mid-print. Everything from
    # BEGIN onward is treated as key material. Over-redaction is the right direction to
    # be wrong in here, for the reason given at the top of this module.
    rule(
        "private_key_unterminated",
        r"(?s)-----BEGIN[^-]{0,40}PRIVATE KEY-----.*\Z",
        "<private-key>",
    ),
    rule(
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+",
        "<jwt>",
    ),
    rule("bearer", r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 <token>"),
    # Credentials inside a connection string. Anchored on :// and @ so an ordinary
    # colon in prose cannot trigger it.
    rule("dsn_credentials", r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)", "<credentials>"),
    rule(
        "assignment",
        r"(?i)\b([a-z_.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|"
        r"access[_-]?key|credential)[a-z_.-]*)\s*[=:]\s*(?!<redacted>)\"?[^\s\"',;]+\"?",
        r"\1=<redacted>",
    ),
    # The same names quoted as JSON keys. Structured loggers write "password": "x", and
    # the assignment rule above needs the key and the separator to touch.
    rule(
        "json_assignment",
        r'(?i)("[a-z_.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|'
        r'access[_-]?key|credential)[a-z_.-]*"\s*:\s*)"(?!<redacted>")[^"]*"',
        r'\1"<redacted>"',
    ),
    rule("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "<aws-key>"),
    # Vendor tokens with fixed, documented prefixes. Prefix-anchored, so an ordinary
    # identifier cannot match by accident.
    rule(
        "github_token",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})",
        "<github-token>",
    ),
    rule("gitlab_token", r"\bglpat-[A-Za-z0-9_-]{20,}", "<gitlab-token>"),
    rule("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "<slack-token>"),
    rule("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b", "<google-api-key>"),
    rule(
        "authorization_header",
        r"(?im)^(.*?\b(?:authorization|x-api-key|set-cookie|cookie))\s*:\s*(?!<redacted>$)\S.*$",
        r"\1: <redacted>",
    ),
)


def redact(text: str | None) -> tuple[str, bool]:
    """Returns the cleaned text and whether anything was removed.

    The boolean drives Signal.redacted, which exists so the assembler can tell a line
    that never contained a secret from one that has been emptied of it. Without it a
    reasoner reading "<redacted>" has no way to know whether that is the tool's
    marker or something the application logged verbatim.
    """
    if not text:
        return text or "", False

    cleaned, fired = apply(RULES, text)
    return cleaned, bool(fired)


def rules_fired(text: str | None) -> tuple[str, ...]:
    """Which rules matched, for a payload field. Names only — a caller that wanted the
    values would be undoing the entire point of this module."""
    if not text:
        return ()
    return apply(RULES, text)[1]
