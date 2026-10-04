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

# Secrets may be listed and described: both show names, types and key sizes, never
# values. Any output format that can print a field can print a Secret's data into a
# terminal's scrollback, so with a Secret in the command only these formats pass.
SECRET_RESOURCES = frozenset({"secret", "secrets"})
SAFE_SECRET_OUTPUTS = frozenset({"name", "wide"})

# Global flags that may sit between kubectl and the verb, and take a separate value.
_VALUE_FLAGS_BEFORE_VERB = frozenset({"-n", "--namespace", "--context"})


def allowed_command_text() -> str:
    """The allowlist as the system prompt states it."""
    return ", ".join(" ".join(c) for c in ALLOWED_COMMANDS)


def secret_rule_text() -> str:
    """The Secret rule as the system prompt states it."""
    outputs = " or ".join(f"-o {o}" for o in sorted(SAFE_SECRET_OUTPUTS))
    return (
        "Secrets may be listed or described, but never printed: with a Secret in the "
        f"command, no output format other than {outputs}, and no --template"
    )


def _output_formats(tokens: list[str]) -> list[str]:
    """Every output format the command asks for, in any spelling kubectl accepts:
    -o yaml, -oyaml, -o=yaml, --output yaml, --output=yaml, and --template."""
    found = []
    for i, token in enumerate(tokens):
        flag, _, inline = token.partition("=")
        if flag in ("-o", "--output"):
            value = inline or (tokens[i + 1] if i + 1 < len(tokens) else "")
            found.append(value.split("=", 1)[0])
        elif token.startswith("-o") and not token.startswith("--") and len(token) > 2:
            found.append(token[2:].lstrip("=").split("=", 1)[0])
        elif flag == "--template":
            found.append("template")
    return found


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

    names_secret = False
    for token in tokens:
        flag = token.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            return f"flag {flag} is not allowed"
        # pods,secrets and secret/name both name a Secret.
        if SECRET_RESOURCES & set(re.split(r"[,/]", token.lower())):
            names_secret = True

    if names_secret:
        printed = [o for o in _output_formats(tokens) if o not in SAFE_SECRET_OUTPUTS]
        if printed:
            return f"printing Secret values (output {printed[0]}) is not allowed"

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
