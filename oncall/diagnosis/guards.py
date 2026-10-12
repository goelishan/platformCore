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
from typing import Any

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

# Flags that change whose credentials or which API server the command uses, read the
# API outside every check below, or print more than the command's own output.
#   - --context, --cluster and --user pick another kubeconfig entry: another cluster or
#     another identity. The engineer is already pointed at the cluster being diagnosed.
#   - --raw sends an arbitrary API path, so `get --raw .../secrets/db` prints a Secret
#     with no resource name or output format for the Secret rule to see.
#   - -v and --v at 8 or above log response bodies, Secret data included; no level is
#     needed to verify a diagnosis, so none is allowed.
FORBIDDEN_FLAGS = (
    "--kubeconfig", "--context", "--cluster", "--user", "--token", "--as", "--as-group",
    "--as-uid", "--server", "-s", "--username", "--password", "--client-key",
    "--client-certificate", "--certificate-authority", "--insecure-skip-tls-verify",
    "--raw", "-v", "--v", "--vmodule",
)

# Secrets may be listed and described: both show names, types and key sizes, never
# values. Any output format that can print a field can print a Secret's data into a
# terminal's scrollback, so with a Secret in the command only these formats pass.
SECRET_RESOURCES = frozenset({"secret", "secrets"})
SAFE_SECRET_OUTPUTS = frozenset({"name", "wide"})

# Impersonation flags are forbidden above, with one exception: `auth can-i` asks the API
# whether an identity may do something (a SubjectAccessReview) and changes nothing. For a
# Job that cannot create pods, "can its ServiceAccount do this?" is the check an engineer
# wants, and on 2026-10-08 the model proposed exactly that and lost a correct diagnosis.
# Placed after the verb only: before it, the parser cannot tell the verb from the value.
IMPERSONATION_FLAGS = frozenset({"--as", "--as-group", "--as-uid"})
_CAN_I = ("auth", "can-i")

# Global flags that may sit between kubectl and the verb, and take a separate value.
_VALUE_FLAGS_BEFORE_VERB = frozenset({"-n", "--namespace"})

# Anything that reads as a link in prose the engineer will see. A model that has been
# steered by text in the evidence most often shows it by sending the reader somewhere.
_URL = re.compile(r"(?i)\b(?:https?|ftp)://|\bwww\.[a-z0-9-]+\.")


def allowed_command_text() -> str:
    """The allowlist as the system prompt states it."""
    return ", ".join(" ".join(c) for c in ALLOWED_COMMANDS)


def forbidden_flag_text() -> str:
    """The forbidden flags as the system prompt states them."""
    return (
        ", ".join(FORBIDDEN_FLAGS)
        + " (" + ", ".join(sorted(IMPERSONATION_FLAGS)) + " are allowed only after "
        + " ".join(_CAN_I) + ")"
    )


def _names_secret(token: str) -> bool:
    """Whether a token names the Secret resource in any spelling kubectl accepts.

    pods,secrets and secret/db are lists and paths; secrets.v1 and secret.v1. carry a
    version and group, which kubectl resolves to the same resource. The part before the
    first dot is the resource. A pod whose own name starts with "secret." is caught too,
    which costs one rejected command and leaks nothing: fail closed.
    """
    return any(
        part.split(".", 1)[0] in SECRET_RESOURCES
        for part in re.split(r"[,/]", token.lower())
    )


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

    rest = _verb_and_rest(tokens)
    can_i = rest is not None and tuple(rest[: len(_CAN_I)]) == _CAN_I
    after_verb = len(tokens) - len(rest) if rest is not None else len(tokens)

    names_secret = False
    for position, token in enumerate(tokens):
        flag = token.split("=", 1)[0]
        if flag in IMPERSONATION_FLAGS and can_i and position > after_verb:
            continue
        # -v8 is -v with its value attached, as pflag allows for any short flag.
        if flag in FORBIDDEN_FLAGS or re.fullmatch(r"-v\d+", flag):
            return f"flag {flag} is not allowed"
        if not token.startswith("-") and _names_secret(token):
            names_secret = True

    if names_secret:
        printed = [o for o in _output_formats(tokens) if o not in SAFE_SECRET_OUTPUTS]
        if printed:
            return f"printing Secret values (output {printed[0]}) is not allowed"

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


def check_prose(diagnosis: Diagnosis) -> list[str]:
    """Free-text fields that carry a link, one line per field.

    The engineer reads summary, titles, mechanisms, unknowns and command purposes, and
    none of them has a reason to point anywhere outside the cluster. Evidence text that
    steered the model most often surfaces as a link in its answer, so a link is a fault
    the model is asked to correct. Commands named in prose are not checked: describing
    how a node came to be cordoned is explanation, and the verify list is where commands
    are held to the allowlist.
    """
    fields = [("summary", diagnosis.summary)]
    fields += [(f"unknowns.{i}", u) for i, u in enumerate(diagnosis.unknowns)]
    for h in diagnosis.hypotheses:
        fields += [(f"hypothesis {h.rank} title", h.title),
                   (f"hypothesis {h.rank} mechanism", h.mechanism)]
        fields += [(f"hypothesis {h.rank} verify purpose", c.purpose) for c in h.verify]
    return [f"{where}: contains a link; the diagnosis must not point outside the evidence"
            for where, text in fields if _URL.search(text)]


# Joins that say a finding is about the subject itself or where it runs. A namespace
# join is co-location only: a Service sharing the namespace.
_DIRECT_JOINS = frozenset({"subject", "owner", "node", "unscheduled", "cluster"})


def check_grounding(
    diagnosis: Diagnosis, findings: Mapping[str, Any]
) -> list[str]:
    """The leading hypothesis has to rest on evidence that bears on the subject.

    findings maps a citation label to its Finding (duck-typed on joined_by and
    observational, so this module stays free of the assembler). Two rules, rank 1 only,
    because that is the answer an engineer acts on first:

      - at least one supporting finding is about the subject or where it runs, not a
        neighbour that merely shares its namespace;
      - at least one supporting finding is evidence, not a report that a source saw
        nothing.

    no_fault is exempt from the first rule only: a healthy subject is shown healthy by
    its own rows, which the second rule still asks for.
    """
    leading = next((h for h in diagnosis.hypotheses if h.rank == 1), None)
    if leading is None:
        return []
    cited = [findings[c] for c in leading.supporting if c in findings]
    if not cited:
        return []

    problems = []
    if str(leading.cause_class) != "no_fault" and not any(
        getattr(f, "joined_by", None) in _DIRECT_JOINS for f in cited
    ):
        problems.append(
            "hypothesis 1: every supporting finding was admitted only via namespace; "
            "cite evidence about the subject itself, or rank this hypothesis lower"
        )
    if all(getattr(f, "observational", False) for f in cited):
        problems.append(
            "hypothesis 1: every supporting finding only reports that a source saw "
            "nothing; cite evidence of the cause, or set insufficient_evidence"
        )
    return problems
