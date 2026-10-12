"""
The command and citation guards.

  - Every rejection case is a command a model has plausibly proposed, or a bypass of the
    obvious check: a write hidden behind a pipe, a verb after a flag, a Secret named
    inside a comma list.
  - The accepted cases include the real commands from the first live diagnosis on
    2026-10-04, so the allowlist is tested against what the model actually writes.
"""

from __future__ import annotations

import pytest

from oncall.diagnosis import guards, prompt
from oncall.diagnosis.schema import Diagnosis


@pytest.mark.parametrize(
    "command",
    [
        "kubectl describe pod stuckrollout-78f8fc8b7-8k2wf -n oncall-lab",
        "kubectl get deployment stuckrollout -n oncall-lab -o yaml",
        "kubectl get replicasets -n oncall-lab -l app=stuckrollout",
        "kubectl get deployment stuckrollout -n oncall-lab "
        "-o jsonpath='{.spec.template.spec.containers[*].image}'",
        "kubectl rollout history deployment stuckrollout -n oncall-lab",
        "kubectl get events -n oncall-lab "
        "--field-selector involvedObject.name=x --sort-by='.lastTimestamp'",
        "kubectl -n oncall-lab logs crashloop-abc --previous",
        "kubectl -n oncall-lab get pods",
        "kubectl top pod -n oncall-lab",
        "kubectl auth can-i list pods",
        "kubectl rollout status deployment/stuckrollout",
        # From the live crashloop diagnosis of 2026-10-04, which the first Secret rule
        # rejected: listing shows names and types, never values.
        "kubectl get secret -n oncall-lab",
        "kubectl describe secret/db -n prod",
        "kubectl get pods,secrets -n prod -o wide",
        "kubectl get secrets -o name",
    ],
)
def test_read_only_commands_are_allowed(command):
    assert guards.check_command(command) is None


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("kubectl delete pod x", "not a read-only command"),
        ("kubectl exec -it x -- sh", "not a read-only command"),
        ("kubectl rollout restart deployment/x", "not a read-only command"),
        ("kubectl rollout undo deployment/x", "not a read-only command"),
        ("kubectl scale deployment x --replicas=0", "not a read-only command"),
        ("kubectl cordon node-1", "not a read-only command"),
        ("kubectl -n prod delete pod x", "not a read-only command"),
        ("kubectl get pods | sh", "shell syntax"),
        ("kubectl get pods; kubectl delete pod x", "shell syntax"),
        ("kubectl get pods && kubectl delete pod x", "shell syntax"),
        ("kubectl get pods > /tmp/x", "shell syntax"),
        ("kubectl get pod $(whoami)", "shell syntax"),
        ("kubectl get pods --kubeconfig=/tmp/admin", "flag --kubeconfig"),
        ("kubectl get pods --as=system:admin", "flag --as"),
        ("kubectl get pods --token abc", "flag --token"),
        ("kubectl get secret db -o yaml", "printing Secret values"),
        ("kubectl get pods,secrets -n prod -o json", "printing Secret values"),
        ("kubectl get secret/db -ojsonpath={.data.password}", "printing Secret values"),
        ("kubectl get secret db --output=go-template={{.data}}", "printing Secret values"),
        ("kubectl get secret db -o=custom-columns=P:.data.password", "printing Secret values"),
        ("kubectl get secret db --output yaml", "printing Secret values"),
        ("kubectl get secret db --template={{.data}}", "printing Secret values"),
        # Found 2026-10-08: a version or group suffix names the same resource, and
        # --raw reads any API path with no resource name for the Secret rule to see.
        ("kubectl get secrets.v1 -n prod -o yaml", "printing Secret values"),
        ("kubectl get secret.v1. db -o json", "printing Secret values"),
        ("kubectl get pods,secrets.v1 -A -o yaml", "printing Secret values"),
        ("kubectl get --raw /api/v1/namespaces/prod/secrets/db", "flag --raw"),
        ("kubectl get --raw=/api/v1/namespaces/prod/secrets/db", "flag --raw"),
        ("kubectl --context prod-eks get pods", "flag --context"),
        ("kubectl get pods --cluster=prod", "flag --cluster"),
        ("kubectl get pods --user admin", "flag --user"),
        ("kubectl get secret db -o name -v=8", "flag -v"),
        ("kubectl get secret db -o name -v8", "flag -v8"),
        ("kubectl get secret db -o name --v=9", "flag --v"),
        ("helm list", "only kubectl"),
        ("kubectl", "no kubectl verb"),
        ("kubectl get 'pods", "cannot be parsed"),
    ],
)
def test_unsafe_or_unknown_commands_are_rejected(command, reason):
    assert reason in (guards.check_command(command) or "")


def test_the_prompt_states_the_allowlist_the_guard_enforces():
    """One list, two readers. If the prompt named a verb the guard rejects, a model that
    followed its instructions would have its diagnosis thrown away."""
    assert guards.allowed_command_text() in prompt.SYSTEM_PROMPT
    assert guards.secret_rule_text() in prompt.SYSTEM_PROMPT
    assert guards.forbidden_flag_text() in prompt.SYSTEM_PROMPT
    for flag in guards.FORBIDDEN_FLAGS:
        assert flag in guards.check_command(f"kubectl get pods {flag} x")
    for allowed in guards.ALLOWED_COMMANDS:
        assert guards.check_command("kubectl " + " ".join(allowed) + " x") is None


def _diagnosis(supporting, contradicting=(), command="kubectl get pods") -> Diagnosis:
    return Diagnosis.model_validate({
        "summary": "s",
        "insufficient_evidence": False,
        "hypotheses": [{
            "rank": 1, "cause_class": "image_pull", "title": "t", "mechanism": "m",
            "confidence": "high", "supporting": list(supporting),
            "contradicting": list(contradicting),
            "verify": [{"command": command, "purpose": "p"}],
        }],
    })


def test_citations_outside_the_bundle_are_reported_once_each():
    d = _diagnosis(["F1", "F9", "F9"], ["F12"])

    assert guards.check_citations(d, {"F1": "a" * 32, "F2": "b" * 32}) == ["F12", "F9"]


def test_citations_inside_the_bundle_pass():
    assert guards.check_citations(_diagnosis(["F1"], ["F2"]), {"F1": "a", "F2": "b"}) == []


def test_every_rejected_command_is_reported_with_its_hypothesis():
    rejected = guards.check_commands(_diagnosis(["F1"], command="kubectl delete pod x"))

    assert len(rejected) == 1
    assert rejected[0].startswith("hypothesis 1: 'kubectl delete pod x'")


def test_a_link_anywhere_in_the_prose_is_reported_by_field():
    d = Diagnosis.model_validate({
        "summary": "Pull fails; see https://evil.example/fix",
        "insufficient_evidence": False,
        "unknowns": ["mirror at www.example.org."],
        "hypotheses": [{
            "rank": 1, "cause_class": "image_pull", "title": "t", "mechanism": "m",
            "confidence": "high", "supporting": ["F1"],
            "verify": [{"command": "kubectl get pods", "purpose": "ftp://x/y"}],
        }],
    })

    problems = guards.check_prose(d)

    assert [p.split(":")[0] for p in problems] == [
        "summary", "unknowns.0", "hypothesis 1 verify purpose",
    ]


def test_prose_without_links_passes_even_when_it_names_a_command():
    """Measured on the 2026-10-04 answers: explaining that a node was cordoned 'via
    kubectl cordon' is correct and common. Only links are faults in prose."""
    d = _diagnosis(["F1"])
    d.hypotheses[0].mechanism = "the node was cordoned via `kubectl cordon` at 10:39"

    assert guards.check_prose(d) == []


class _F:
    def __init__(self, joined_by="owner", observational=False):
        self.joined_by = joined_by
        self.observational = observational


def test_a_leading_hypothesis_resting_only_on_neighbours_is_sent_back():
    problems = guards.check_grounding(_diagnosis(["F1", "F2"]),
                                      {"F1": _F("namespace"), "F2": _F("namespace")})

    assert len(problems) == 1
    assert "only via namespace" in problems[0]


def test_one_direct_citation_is_enough():
    assert guards.check_grounding(_diagnosis(["F1", "F2"]),
                                  {"F1": _F("namespace"), "F2": _F("node")}) == []


def test_a_leading_hypothesis_resting_only_on_blind_sources_is_sent_back():
    problems = guards.check_grounding(_diagnosis(["F1"]), {"F1": _F(observational=True)})

    assert len(problems) == 1
    assert "saw nothing" in problems[0]


def test_lower_ranks_are_not_held_to_grounding():
    d = Diagnosis.model_validate({
        "summary": "s", "insufficient_evidence": False,
        "hypotheses": [
            {"rank": r, "cause_class": "image_pull", "title": "t", "mechanism": "m",
             "confidence": "low", "supporting": [f"F{r}"],
             "verify": [{"command": "kubectl get pods", "purpose": "p"}]}
            for r in (1, 2)
        ],
    })

    assert guards.check_grounding(d, {"F1": _F(), "F2": _F("namespace", True)}) == []


def test_no_fault_may_rest_on_a_neighbour_but_not_on_blindness():
    d = _diagnosis(["F1"])
    d.hypotheses[0].cause_class = "no_fault"

    assert guards.check_grounding(d, {"F1": _F("namespace")}) == []
    assert guards.check_grounding(d, {"F1": _F("namespace", True)}) != []


def test_every_identity_server_and_raw_flag_stays_forbidden():
    """Pinned by name. The prompt and the guard read one list, so a flag dropped from it
    drops from both and no other test notices; this one does."""
    assert set(guards.FORBIDDEN_FLAGS) >= {
        "--kubeconfig", "--context", "--cluster", "--user", "--token", "--as",
        "--as-group", "--as-uid", "--server", "-s", "--username", "--password",
        "--client-key", "--client-certificate", "--certificate-authority",
        "--insecure-skip-tls-verify", "--raw", "-v", "--v", "--vmodule",
    }


@pytest.mark.parametrize("command", [
    "kubectl auth can-i create pods -n oncall-lab "
    "--as=system:serviceaccount:oncall-lab:failingcronjob",
    "kubectl auth can-i list jobs --as system:serviceaccount:oncall-lab:default "
    "--as-group system:serviceaccounts",
    "kubectl auth can-i get nodes --as-uid=1001 --as=jane",
])
def test_impersonation_is_allowed_to_ask_can_i(command):
    """can-i is a SubjectAccessReview: it asks whether an identity may act and changes
    nothing, and for a ServiceAccount that cannot create pods it is the check to run."""
    assert guards.check_command(command) is None


@pytest.mark.parametrize("command, flag", [
    ("kubectl get secrets -n oncall-lab --as admin", "--as"),
    ("kubectl describe pod web -n oncall-lab --as-uid 0", "--as-uid"),
    ("kubectl auth whoami --as=admin", "--as"),
    # Before the verb the parser cannot tell the identity from the verb, so the
    # exception holds only after it.
    ("kubectl --as admin auth can-i get pods", "--as"),
    ("kubectl --as=admin auth can-i get pods", "--as"),
])
def test_impersonation_anywhere_else_is_rejected(command, flag):
    assert guards.check_command(command) == f"flag {flag} is not allowed"
