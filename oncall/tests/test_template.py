"""
Identity keys derived from free text.

Assertions are on the key, not on the placeholder text, wherever the question is
"do these group together". Asserting the exact template would fail every time a
placeholder is renamed, which changes nothing about behaviour, and would pass while
two messages that must not group produced the same key.
"""

from __future__ import annotations

import pytest

from oncall.envelope import NORMALIZER_VERSION, template_of
from oncall.envelope.template import MAX_TEMPLATE_CHARS


# ---- the two failures this module exists to fix ----------------------------
# Both came from real collectors: k8s_pods keyed every unschedulable pod alike, and
# k8s_events could not admit message into its key without a fingerprint per pod IP.


def test_unschedulable_causes_do_not_merge():
    taint = template_of(
        "0/3 nodes are available: 1 node(s) had untolerated taint "
        "{node-role.kubernetes.io/control-plane: }, 2 Insufficient cpu."
    )
    memory = template_of("0/3 nodes are available: 3 Insufficient memory.")

    assert taint.key != memory.key


def test_probe_failure_survives_a_changed_pod_ip():
    first = template_of(
        'Liveness probe failed: Get "http://10.244.0.7:8080/healthz": '
        "context deadline exceeded"
    )
    rescheduled = template_of(
        'Liveness probe failed: Get "http://10.244.3.19:8080/healthz": '
        "context deadline exceeded"
    )

    assert first.key == rescheduled.key


def test_different_probe_paths_stay_apart():
    """The authority is masked, the path is not. A readiness probe and a liveness
    probe against one pod are two problems with two fixes."""
    liveness = template_of('Get "http://10.244.0.7:8080/healthz": timeout')
    readiness = template_of('Get "http://10.244.0.7:8080/readyz": timeout')

    assert liveness.key != readiness.key


# ---- individual rules ------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("started at 2026-08-14T03:14:07Z", "started at 2026-08-11T22:01:59.412Z"),
        ("uid 9f1c2b44-1f2e-4a77-9c31-2b5f7f0e1a44",
         "uid 0a3d7e21-cc90-4b02-81ee-77c9d4b31f05"),
        ("cannot reach 10.96.0.1:443", "cannot reach 172.20.31.8:443"),
        ("pod nginx-7d9f8b6c4d-x2ktp evicted", "pod nginx-5c8b7a9e2f-q7wzp evicted"),
        ("container id docker://a1b2c3d4e5f6a7b8", "container id docker://ff00aa11bb22cc33"),
        ("limit of 512Mi exceeded", "limit of 2Gi exceeded"),
        ("timed out after 10s", "timed out after 250ms"),
        ("restarted 5 times", "restarted 41 times"),
    ],
)
def test_varying_tokens_do_not_split_one_problem(left, right):
    assert template_of(left).key == template_of(right).key


def test_ipv4_is_masked_as_an_address_not_as_four_numbers():
    """Ordering check. If the number rule ran first the address would survive as
    <n>.<n>.<n>.<n>, which is a stable key and a useless one: every address in the
    cluster would then template identically to every version string."""
    assert "<ip>" in template_of("dial tcp 10.96.0.1:443 refused").text


def test_hex_rule_leaves_ordinary_words_alone():
    """deadbeef and defaced are spellable entirely from hex letters. The rule requires
    a digit for exactly this reason, and a merge here would be silent."""
    assert template_of("the cache was defaced").text == "the cache was defaced"


# ---- properties ------------------------------------------------------------


def test_templating_is_idempotent():
    """Placeholders contain no digits, so a second pass has nothing left to match.
    This is what makes it safe for the retrofit to run over text that may already have
    passed through: a rule that consumed its own output would produce a different key
    depending on how many times it had been applied."""
    once = template_of("pod web-7d9f8b6c4d-x2ktp used 512Mi at 2026-08-14T03:14:07Z")
    twice = template_of(once.text)

    assert twice.text == once.text
    assert twice.key == once.key


def test_template_is_bounded():
    long_message = "connection refused to backend " + ("x" * 4000)
    assert len(template_of(long_message).text) == MAX_TEMPLATE_CHARS


def test_whitespace_is_collapsed_so_reflowed_messages_agree():
    flat = template_of("failed to pull image: not found")
    wrapped = template_of("failed to pull image:\n    not found\n")

    assert flat.key == wrapped.key


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t "])
def test_missing_message_yields_an_empty_key_not_an_error(empty):
    """A message is optional on most Kubernetes objects. All the ways of having none
    must produce one identity, or "no message" and "a message of three spaces" become
    two different problems permanently."""
    result = template_of(empty)

    assert result.text == ""
    assert result.key == ""


def test_pod_identity_in_the_kubernetes_event_form_is_masked():
    """Events name their subject as pod_namespace(uid). The underscore suppresses the
    word boundary the suffix rule would otherwise end on, so this form is the one that
    breaks first — and it is the form the events collector sees most."""
    first = template_of("Back-off restarting failed container app in pod "
                        "web-7d9f8b6c4d-x2ktp_lab(9f1c2b44-1f2e-4a77-9c31-2b5f7f0e1a44)")
    second = template_of("Back-off restarting failed container app in pod "
                         "web-5c8b7a9e2f-q7wzp_lab(0a3d7e21-cc90-4b02-81ee-77c9d4b31f05)")

    assert first.key == second.key
    assert "<rand>" in first.text


def test_version_travels_with_every_template():
    assert template_of("anything at all").version == NORMALIZER_VERSION


def test_fired_rules_are_reported_by_name():
    """The names are payload material. They say what kind of token was removed without
    restating the token, which is what makes a key debuggable after the fact."""
    fired = template_of("pod web-1 on 10.0.0.5 died after 30s").masked

    assert "ipv4" in fired
    assert "duration" in fired
