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
from collections.abc import Collection, Mapping
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


def _screen(command: str) -> tuple[str | None, list[str], list[str] | None]:
    """The rules every suggested command is held to, read-only or not: one kubectl
    invocation, no shell, no other identity or server, no Secret printed. Returns the
    reason it fails (or None), its tokens and the tokens from the verb onward."""
    for meta in SHELL_METACHARACTERS:
        if meta in command:
            return f"shell syntax {meta!r} is not allowed", [], None
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return f"cannot be parsed: {e}", [], None
    if not tokens or tokens[0] != "kubectl":
        return "only kubectl commands are allowed", tokens, None

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
            return f"flag {flag} is not allowed", tokens, rest
        if not token.startswith("-") and _names_secret(token):
            names_secret = True

    if names_secret:
        printed = [o for o in _output_formats(tokens) if o not in SAFE_SECRET_OUTPUTS]
        if printed:
            return f"printing Secret values (output {printed[0]}) is not allowed", tokens, rest

    if rest is None:
        return "no kubectl verb", tokens, rest
    return None, tokens, rest


def check_command(command: str) -> str | None:
    """None when the command is allowed, otherwise the reason it is not."""
    reason, _, rest = _screen(command)
    if reason or rest is None:
        return reason
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
        for i, step in enumerate(h.remediate, 1):
            fields += [(f"hypothesis {h.rank} remediate {i} {name}", text)
                       for name, text in (("action", step.action), ("undo", step.undo))
                       if text]
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


def namespaces_in(command: str) -> list[str]:
    """Every namespace a command names with -n or --namespace, in any spelling kubectl
    accepts: -n x, -nx, -n=x, --namespace x, --namespace=x. A command that does not parse
    names none here; check_command has already rejected it."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    found = []
    for i, token in enumerate(tokens):
        flag, eq, inline = token.partition("=")
        if flag in ("-n", "--namespace"):
            value = inline if eq else (tokens[i + 1] if i + 1 < len(tokens) else "")
        elif token.startswith("-n") and not token.startswith("--") and len(token) > 2:
            value = token[2:]
        else:
            continue
        if value:
            found.append(value)
    return found


def check_namespaces(diagnosis: Diagnosis, namespaces: Collection[str]) -> list[str]:
    """Commands that point at a namespace the evidence never mentions, one line each.

    check_command decides whether a command is safe; this decides whether it can be
    about this incident. On 2026-10-13 a whole-cluster diagnosis sent the engineer to
    -n loki while Loki ran in monitoring: a read-only command that answers nothing, at
    the moment someone is relying on it. The model can only know a namespace from the
    evidence, so a namespace outside it is a guess and earns a repair. -A is unaffected.
    """
    allowed = set(namespaces)
    hint = (f"use one of {', '.join(sorted(allowed))}" if allowed
            else "the evidence names no namespace, so drop -n or use -A")
    problems = []
    for h in diagnosis.hypotheses:
        for c in h.verify:
            for ns in namespaces_in(c.command):
                if ns not in allowed:
                    problems.append(
                        f"hypothesis {h.rank}: {c.command!r}: namespace {ns} appears "
                        f"nowhere in the evidence; {hint}"
                    )
    return problems



# ---- remedy steps ------------------------------------------------------------------
# A remedy changes the cluster, so it is held to a different list from verify, and it is
# never run by anything in this package: the agent has no write path at all, and a remedy
# reaches the cluster only when the engineer types it. These rules exist so that what the
# engineer is shown is honest about its blast radius, not to make it safe to automate.
#
# Each verb carries the lowest risk it may be labelled with. A model that calls a drain
# "low" is understating what the engineer is about to do; the floor makes that a repair
# rather than a label the engineer has to second-guess at 3am.

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

REMEDY_COMMANDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("rollout", "restart"), "medium"),
    (("rollout", "undo"), "medium"),
    (("rollout", "pause"), "low"),
    (("rollout", "resume"), "low"),
    (("scale",), "medium"),
    (("autoscale",), "medium"),
    (("set", "image"), "medium"),
    (("set", "env"), "medium"),
    (("set", "resources"), "medium"),
    (("patch",), "medium"),
    (("edit",), "medium"),
    (("label",), "low"),
    (("annotate",), "low"),
    (("uncordon",), "low"),
    # Stops new pods landing; evicts nothing and uncordon undoes it. Medium until the
    # v7 eval, where the model labelled it low and was right.
    (("cordon",), "low"),
    (("taint",), "medium"),
    (("drain",), "high"),
    # Additive: a new object nothing referenced before, undone by deleting it.
    (("create", "configmap"), "low"),
    # One named pod or Job: its controller recreates it, or it is disposable by design.
    # Raised to high below for a forced delete or one that matches many objects. Medium
    # until the v6 eval of 2026-10-29, where the model labelled a single pod delete low
    # on 4 of 17 bundles and was right each time.
    (("delete",), "low"),
)

# What delete may name. A pod or a Job is recreated by its owner or is disposable by
# design; anything else (a Deployment, a PVC, a namespace) loses state or configuration
# that the evidence cannot show is safe to lose, so it is described, never commanded.
DELETABLE = frozenset({"pod", "pods", "po", "job", "jobs"})

# Deletion that skips graceful termination. The remedy for a pod stuck terminating, and
# also the way to leave a volume attached to a node that never saw the pod stop.
_FORCED = frozenset({"--force", "--grace-period=0", "--now"})


# Deletion of every match rather than of one named object.
_MASS = frozenset({"--all", "-l", "--selector", "--field-selector", "-A", "--all-namespaces"})

# Flags whose value is a separate token, so the value is not mistaken for the resource.
_VALUE_FLAGS = frozenset({"-n", "--namespace", "-l", "--selector", "--field-selector",
                          "-o", "--output", "-f", "--filename", "--grace-period", "--timeout"})


def _first_positional(tokens: list[str]) -> str:
    skip = False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            skip = t in _VALUE_FLAGS
            continue
        return t
    return ""


def remedy_text() -> str:
    """The remedy allowlist with its risk floors, as the system prompt states it."""
    by_floor: dict[str, list[str]] = {}
    for verb, floor in REMEDY_COMMANDS:
        by_floor.setdefault(floor, []).append(" ".join(verb))
    parts = [f"{floor}: {', '.join(verbs)}" for floor, verbs in
             sorted(by_floor.items(), key=lambda kv: RISK_ORDER[kv[0]])]
    return (
        "; ".join(parts)
        + f". delete may name only {', '.join(sorted(DELETABLE))}; a forced delete "
        "(--force, --grace-period=0) or one by selector or --all is high. A value the "
        "evidence does not show may be a placeholder in angle brackets, such as <tag>"
    )


# A value the evidence does not show, written for the engineer to fill in: <tag>,
# <registry-host>, <pod-name>. Allowed in remedy commands only. A remedy is read and typed
# by a person, so a placeholder is more honest than a guessed image tag; a verify command
# is run as written, so it stays strict. In the v6 eval, 4 of 17 bundles lost a repair to
# a placeholder rejected as shell redirection.
PLACEHOLDER = re.compile(r"<[a-z][a-z0-9-]{0,40}>")
_PLACEHOLDER_STAND_IN = "PLACEHOLDER"


def is_placeholder(token: str) -> bool:
    return bool(PLACEHOLDER.fullmatch(token))


def check_remedy_command(command: str) -> tuple[str | None, str]:
    """(reason it is not allowed or None, the lowest risk it may be labelled)."""
    reason, tokens, rest = _screen(PLACEHOLDER.sub(_PLACEHOLDER_STAND_IN, command))
    if reason or rest is None:
        return reason, "high"
    for verb, floor in REMEDY_COMMANDS:
        if tuple(rest[: len(verb)]) != verb:
            continue
        if verb == ("delete",):
            target = _first_positional(rest[1:])
            if target.split("/", 1)[0].lower() not in DELETABLE:
                return (f"delete may name only {', '.join(sorted(DELETABLE))}; describe "
                        "any other deletion as an action without a command"), "high"
            flags = {t.split("=", 1)[0] if t.startswith(("--force", "--all")) else t
                     for t in tokens}
            if _FORCED & flags or _MASS & flags:
                floor = "high"
        return None, floor
    return (f"'{' '.join(rest[:2])}' is not on the remedy allowlist; describe the step "
            "as an action without a command"), "high"


def check_remedies(diagnosis: Diagnosis) -> list[str]:
    """Problems with the remedy steps, one line each. All earn a repair: unlike verify,
    a remedy is expected to change the cluster, so a write is not a sign of a misread
    incident. What is checked is whether the step is honest and usable.

      - every hypothesis that names a fault offers at least one step;
      - a command is one the remedy allowlist knows, labelled no lower than its floor;
      - medium and high steps say how to undo them;
      - no credential appears in a step: the engineer will paste it into a shell
        history, and the journal would store it.
    """
    from oncall.envelope.redact import rules_fired

    problems = []
    for h in diagnosis.hypotheses:
        where = f"hypothesis {h.rank}"
        if str(h.cause_class) != "no_fault" and not h.remediate:
            problems.append(f"{where}: remediate must give at least one step for a fault")
        for i, step in enumerate(h.remediate, 1):
            at = f"{where} remediate {i}"
            if step.command:
                reason, floor = check_remedy_command(step.command)
                if reason:
                    problems.append(f"{at}: {step.command!r}: {reason}")
                elif RISK_ORDER[step.risk] < RISK_ORDER[floor]:
                    problems.append(
                        f"{at}: {step.command!r} is labelled {step.risk}, below its floor "
                        f"of {floor}"
                    )
            if step.risk != "low" and not step.undo:
                problems.append(f"{at}: a {step.risk} risk step must say how to undo it")
            # Command and undo, which are pasted into a shell. Not the action: evidence is
            # redacted before the model sees it, and prose like "the password: field of
            # the Secret" tripped the assignment rule on a correct step (v7, crashloop).
            fired = rules_fired(" ".join(filter(None, (step.command, step.undo))))
            if fired:
                problems.append(
                    f"{at}: looks like it carries a credential ({', '.join(fired)}); "
                    "reference the Secret that holds it instead"
                )
    return problems


def check_remedy_namespaces(diagnosis: Diagnosis, namespaces: Collection[str]) -> list[str]:
    """The namespace rule for remedy commands, which matter more than verify: a remedy
    aimed at the wrong namespace changes something nobody was diagnosing."""
    allowed = set(namespaces)
    return [
        f"hypothesis {h.rank} remediate {i}: {step.command!r}: namespace {ns} appears "
        "nowhere in the evidence"
        for h in diagnosis.hypotheses
        for i, step in enumerate(h.remediate, 1) if step.command
        for ns in namespaces_in(step.command)
        if ns not in allowed and not is_placeholder(ns)
    ]
