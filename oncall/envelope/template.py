"""
The stable part of a free-text message, and the key derived from it.

  - Collectors key on causes, not on messages. A message carries the cause wrapped in
    tokens that change every occurrence — an IP, a pod suffix, a byte count — and a
    key built from the raw text mints a new identity for every occurrence of one
    problem. Stripping the varying tokens leaves something that repeats.
  - Masking only. No curated per-reason extractors: a curated set is sharpest on the
    failures already understood and blurriest on novel ones, which is backwards for
    triage, and adding an extractor later re-keys every message it now claims. Where
    a collector wants precision it puts structured fields in the payload, where a
    query can revise them.
  - The rule set is deliberately small. An unmasked varying token splits one problem
    into many, which is visible as a query — many fingerprints, one occurrence each,
    same subject and window. An over-broad rule merges two problems into one, which
    is invisible, because a wrong group looks exactly like a right one. Under doubt,
    leave the token in.
  - NORMALIZER_VERSION travels with every row. Changing these rules changes the key
    for some population of messages, so recurrence history and first_seen_ever break
    at the deploy boundary with nothing raising. The version does not prevent that —
    nothing can, since the improvement and the fracture are one event — it makes the
    seam legible to a query instead of silent.
"""

from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

from oncall.envelope.masking import Rule, apply, rule

# Incremented by hand when RULES change in a way that moves keys. Deliberately not
# derived from hashing the rule set: a derived version bumps on a comment edit or a
# reordering that changes nothing, and a version that moves for no reason is one
# nobody trusts enough to query on.
NORMALIZER_VERSION = 1

# Bounds the hash input so one pathological message cannot dominate. Truncation
# merges, which is the direction this module otherwise avoids, so the cap sits far
# above any real reason string and applies after masking, when the text is at its
# shortest.
MAX_TEMPLATE_CHARS = 512


# ---- rules -----------------------------------------------------------------
# Narrow before broad, and every rule that consumes digits before the bare number
# rule. An IP reaching <n> as four separate numbers still produces a stable key, but
# a useless one: every IPv4 address in the cluster would template identically to
# every version string.
#
# Kept out on purpose:
#   IPv6, because a pattern loose enough to catch it also catches ordinary
#     colon-separated text, and a wrong merge costs more than a split.
#   Quoted strings, because the quoted part is usually the diagnosis
#     (Get "http://.../healthz" -> the path is what distinguishes probes).
#   Filesystem paths, because a path names which mount or which config is wrong.
#     Numeric segments inside one are handled by the number rule.

RULES: tuple[Rule, ...] = (
    rule(
        "timestamp",
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
        "<ts>",
    ),
    rule(
        "uuid",
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "<uuid>",
    ),
    # Port folded in with the address. Split apart, the port would fall to <n> and a
    # message naming two different services on one host would template identically.
    rule("ipv4", r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b", "<ip>"),
    # Authority only. The scheme and the path survive, because http vs https and
    # /healthz vs /readyz are both things that distinguish one failure from another.
    rule("host", r"(?<=://)[^/\s\"]+", "<host>"),
    # At least one digit required, so ordinary words made only of hex letters
    # (deadbeef, defaced) are not swallowed.
    rule("hex", r"\b(?=[0-9a-f]*[0-9])[0-9a-f]{8,}\b", "<hex>"),
    # ReplicaSet hashes and pod suffixes: a trailing run mixing letters and digits.
    # Both leading lookaheads are needed — letters-only is a word, digits-only is a
    # number the number rule should handle in its own right.
    #
    # Terminated by (?![a-z0-9]) rather than \b. Kubernetes writes pod identity as
    # name_namespace(uid), and an underscore is a word character, so \b does not exist
    # between the suffix and what follows it. The rule would then miss precisely the
    # form event messages use, and the number rule would reach inside the suffix and
    # rewrite x2ktp as x<n>ktp — a different key per pod, which is the split this rule
    # was written to prevent.
    rule(
        "rand_suffix",
        r"-(?=[a-z0-9]*[0-9])(?=[a-z0-9]*[a-z])[a-z0-9]{5,10}(?![a-z0-9])",
        "-<rand>",
    ),
    rule("size", r"\b\d+(?:\.\d+)?(?:Ki|Mi|Gi|Ti|[KMGT]B|B)\b", "<size>"),
    # Compound durations are one token, not several. Go's duration formatting — which
    # is what the kubelet emits — writes 5m0s and 1h30m0s, and there is no word
    # boundary between 'm' and '0', so a single-unit pattern matches neither half. The
    # back-off interval in "Back-off 5m0s restarting failed container" would then reach
    # the number rule as two separate integers, and 10s and 5m0s would template
    # differently: one recurring problem split by how long the kubelet had been backing
    # off, which is the exact thing the template exists to absorb.
    #
    # The trailing lookahead stops "2meters" from being read as a 2-minute duration.
    rule(
        "duration",
        r"\b\d+(?:\.\d+)?(?:ns|us|µs|ms|[smh])"
        r"(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|[smh]))*(?![a-zA-Z0-9])",
        "<dur>",
    ),
    rule("number", r"\d+(?:\.\d+)?", "<n>"),
)

_WHITESPACE = re.compile(r"\s+")


# ---- output ----------------------------------------------------------------


class Template(NamedTuple):
    """text is for the payload, key is for dedupe_key.

    Both, not one. The key is a hash because dedupe_key is joined into the fingerprint
    basis with '|' and an arbitrary message would inject the separator and grow the
    basis without bound. A hash alone would leave nobody able to see why two things
    grouped, so the readable form travels beside it in the payload where it costs
    nothing and answers that question directly.
    """

    text: str
    key: str
    version: int
    masked: tuple[str, ...]


def template_of(text: str | None) -> Template:
    """Empty and missing messages return an empty template rather than raising.

    A message is optional on most Kubernetes objects, and a collector that has none
    still has a container and a reason to key on. The empty key is distinct from any
    real one, so signals with no message group together instead of merging with
    whatever happened to template to nothing.

    Whitespace-only counts as absent. Falling through would hash the empty string and
    hand a blank message a real-looking key, so "no message" and "a message of three
    spaces" would identify as two different problems for the rest of time.
    """
    if not text or not text.strip():
        return Template("", "", NORMALIZER_VERSION, ())

    masked, fired = apply(RULES, text)

    # Collapsed after masking, not before. Newlines inside a message are formatting,
    # and a multi-line message that is reflowed by its emitter would otherwise
    # template two ways.
    collapsed = _WHITESPACE.sub(" ", masked).strip()[:MAX_TEMPLATE_CHARS]

    key = hashlib.sha256(collapsed.encode()).hexdigest()[:16]
    return Template(collapsed, key, NORMALIZER_VERSION, fired)
