"""
Checks on a parsed diagnosis that the model cannot be trusted to make itself.

  - Pure functions, no I/O. Each returns what it rejected, so the caller decides what a
    rejection costs: a bad citation earns a repair attempt, an unsafe command does not.
  - ALLOWED_COMMANDS is the single list of what a suggested command may do. The system
    prompt is generated from it, so the instruction the model reads and the rule it is
    held to cannot drift apart.
  - Fail closed. A command is accepted only when it is recognisably one of the allowed
    read-only forms; anything unparsed, unknown or ambiguous is rejected.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

from oncall.diagnosis.schema import Diagnosis

ALLOWED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("get",),
    ("describe",),
    ("logs",),
    ("top",),
    ("events",),
    ("explain",),
    ("auth", "can-i"),
    ("rollout", "history"),
    ("rollout", "status"),
)

# Shell syntax that turns one command into several, or sends its output somewhere.
SHELL_METACHARACTERS = (";", "|", "&", "`", "$(", ">", "<", "\n")

# Flags that change whose credentials or which API server the command uses.
FORBIDDEN_FLAGS = (
    "--kubeconfig", "--token", "--as", "--as-group", "--as-uid", "--server", "-s",
    "--username", "--password", "--client-key", "--client-certificate",
    "--certificate-authority", "--insecure-skip-tls-verify",
)

# Read-only by verb, and still not something a triage suggestion should print to a
# terminal: Secret data is readable through get -o yaml.
FORBIDDEN_RESOURCES = frozenset({"secret", "secrets"})

# Global flags that may sit between kubectl and the verb, and take a separate value.
_VALUE_FLAGS_BEFORE_VERB = frozenset({"-n", "--namespace", "--context"})


def allowed_command_text() -> str:
    """The allowlist as the system prompt states it."""
    return ", ".join(" ".join(c) for c in ALLOWED_COMMANDS)


def _verb_and_rest(tokens: list[str]) -> list[str] | None:
    """Tokens from the verb onward, skipping the global flags allowed before it."""
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] in _VALUE_FLAGS_BEFORE_VERB:
            i += 1
        i += 1
    return tokens[i:] if i < len(tokens) else None


def check_command(command: str) -> str | None:
    """None when the command is allowed, otherwise the reason it is not."""
    for meta in SHELL_METACHARACTERS:
        if meta in command:
            return f"shell syntax {meta!r} is not allowed"
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return f"cannot be parsed: {e}"
    if not tokens or tokens[0] != "kubectl":
        return "only kubectl commands are allowed"

    for token in tokens:
        flag = token.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            return f"flag {flag} is not allowed"
        # pods,secrets and secret/name both name a Secret.
        if FORBIDDEN_RESOURCES & set(re.split(r"[,/]", token.lower())):
            return "reading Secrets is not allowed"

    rest = _verb_and_rest(tokens)
    if rest is None:
        return "no kubectl verb"
    for allowed in ALLOWED_COMMANDS:
        if tuple(rest[: len(allowed)]) == allowed:
            return None
    return f"'{' '.join(rest[:2])}' is not a read-only command on the allowlist"


def check_commands(diagnosis: Diagnosis) -> list[str]:
    """Every rejected verify command, with its reason."""
    rejected = []
    for h in diagnosis.hypotheses:
        for c in h.verify:
            reason = check_command(c.command)
            if reason:
                rejected.append(f"hypothesis {h.rank}: {c.command!r}: {reason}")
    return rejected


def check_citations(diagnosis: Diagnosis, labels: Mapping[str, str]) -> list[str]:
    """Cited IDs that are not findings in this bundle, sorted, each once."""
    cited = {c for h in diagnosis.hypotheses for c in (*h.supporting, *h.contradicting)}
    return sorted(cited - set(labels))
