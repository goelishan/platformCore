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
        "kubectl --context kind-oncall get nodes",
        "kubectl top pod -n oncall-lab",
        "kubectl auth can-i list pods",
        "kubectl rollout status deployment/stuckrollout",
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
        ("kubectl get secret db -o yaml", "Secrets"),
        ("kubectl get pods,secrets -n prod", "Secrets"),
        ("kubectl describe secret/db", "Secrets"),
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
