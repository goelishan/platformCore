"""
The substitution primitive shared by identity and redaction.

  - Rules are ordered and applied in sequence. Order is load-bearing: a broad rule
    placed ahead of a narrow one consumes the text the narrow one exists to
    recognise, and the result still looks plausible, so the mistake is silent.
  - A rule replaces with a named placeholder rather than deleting. Deleting would let
    two messages that differ only in where a token sat collapse into each other.
  - apply() reports which rules fired, never what they matched. That is what lets a
    caller record *that* something was replaced without re-introducing the value it
    just removed.
  - Pure, and imports nothing outside the standard library. Both callers sit on the
    write path, which is the one part of this agent that must not acquire new ways
    to fail.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Rule(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    placeholder: str


def rule(name: str, pattern: str, placeholder: str) -> Rule:
    """Compiled once at import. These run per message on every collection cycle, and
    re.sub against a string pattern recompiles through a cache lookup each call."""
    return Rule(name, re.compile(pattern), placeholder)


def apply(rules: tuple[Rule, ...], text: str) -> tuple[str, tuple[str, ...]]:
    """Returns the rewritten text and the names of the rules that actually matched.

    subn rather than sub because the count is the only evidence a rule fired. Testing
    whether the output changed would miss a rule that replaced a token with something
    textually identical, and inferring it from a second search would run every pattern
    twice.
    """
    fired: list[str] = []

    for r in rules:
        text, hits = r.pattern.subn(r.placeholder, text)
        if hits:
            fired.append(r.name)

    return text, tuple(fired)
