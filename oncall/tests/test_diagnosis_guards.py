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


# ---- namespaces the evidence names (F4) -----------------------------------------


@pytest.mark.parametrize(("command", "names"), [
    ("kubectl get pods -n monitoring", ["monitoring"]),
    ("kubectl get pods -nmonitoring", ["monitoring"]),
    ("kubectl get pods -n=monitoring", ["monitoring"]),
    ("kubectl -n monitoring logs x", ["monitoring"]),
    ("kubectl get pods --namespace monitoring", ["monitoring"]),
    ("kubectl get pods --namespace=monitoring", ["monitoring"]),
    ("kubectl get pods -A", []),
    ("kubectl get nodes", []),
    ("kubectl get pods --sort-by=.metadata.name", []),
])
def test_every_spelling_of_a_namespace_flag_is_read(command, names):
    assert guards.namespaces_in(command) == names


def test_a_namespace_absent_from_the_evidence_is_sent_back_with_the_ones_present():
    """The 2026-10-13 whole-cluster diagnosis: -n loki, while Loki ran in monitoring."""
    d = _diagnosis(["F1"], command="kubectl get pods -n loki")

    problems = guards.check_namespaces(d, {"monitoring", "platformcore"})

    assert len(problems) == 1
    assert "namespace loki appears nowhere in the evidence" in problems[0]
    assert "monitoring, platformcore" in problems[0]


def test_a_namespace_in_the_evidence_passes_and_no_namespace_is_never_a_problem():
    assert guards.check_namespaces(
        _diagnosis(["F1"], command="kubectl get pods -n monitoring"), {"monitoring"}) == []
    assert guards.check_namespaces(_diagnosis(["F1"], command="kubectl get nodes"), set()) == []


def test_evidence_without_namespaces_admits_no_namespace_flag():
    problems = guards.check_namespaces(
        _diagnosis(["F1"], command="kubectl get pods -n kube-system"), set())

    assert problems and "drop -n or use -A" in problems[0]


# ---- remedy steps -----------------------------------------------------------------


def _with_remedy(*steps, cause="image_pull") -> Diagnosis:
    d = _diagnosis(["F1"]).model_dump(mode="json")
    d["hypotheses"][0]["cause_class"] = cause
    d["hypotheses"][0]["remediate"] = [
        {"action": "do it", "risk": "medium", "undo": "undo it", **s} for s in steps
    ]
    return Diagnosis.model_validate(d)


@pytest.mark.parametrize(("command", "floor"), [
    ("kubectl rollout undo deployment/stuckrollout -n oncall-lab", "medium"),
    ("kubectl rollout restart deployment web", "medium"),
    ("kubectl -n oncall-lab scale deployment web --replicas=2", "medium"),
    ("kubectl set image deployment/web app=nginx:1.27", "medium"),
    ("kubectl create configmap app-config -n oncall-lab --from-literal=LEVEL=info", "low"),
    ("kubectl uncordon oncall-worker2", "low"),
    ("kubectl label pod x tier=web", "low"),
    ("kubectl drain node-1 --ignore-daemonsets", "high"),
    ("kubectl delete pod crashloop-abc -n oncall-lab", "low"),
    ("kubectl -n oncall-lab delete pod crashloop-abc", "low"),
    ("kubectl delete -n oncall-lab pod crashloop-abc", "low"),
    ("kubectl delete job/nightly-export", "low"),
    ("kubectl delete pod stuck-0 --grace-period=0 --force", "high"),
    ("kubectl delete pods -l app=web", "high"),
    ("kubectl delete pods --all -n oncall-lab", "high"),
])
def test_remedy_commands_on_the_allowlist_carry_their_floor(command, floor):
    assert guards.check_remedy_command(command) == (None, floor)


@pytest.mark.parametrize(("command", "reason"), [
    ("kubectl delete deployment web", "delete may name only"),
    ("kubectl delete namespace oncall-lab", "delete may name only"),
    ("kubectl -n oncall-lab delete pvc data-0", "delete may name only"),
    ("kubectl delete -f app.yaml", "delete may name only"),
    ("kubectl exec -it web -- sh", "not on the remedy allowlist"),
    ("kubectl apply -f fix.yaml", "not on the remedy allowlist"),
    ("kubectl create secret generic db --from-literal=p=x", "not on the remedy allowlist"),
    ("kubectl rollout restart deployment web | tee log", "shell syntax"),
    ("kubectl --context prod rollout restart deployment web", "flag --context"),
    ("helm rollback web 3", "only kubectl"),
])
def test_remedy_commands_off_the_allowlist_are_named_with_a_reason(command, reason):
    found, _ = guards.check_remedy_command(command)

    assert found is not None and reason in found


def test_the_remedy_prompt_states_the_allowlist_the_guard_enforces():
    text = prompt.REMEDY_SYSTEM_PROMPT

    assert guards.remedy_text() in text
    for verb, _ in guards.REMEDY_COMMANDS:
        assert " ".join(verb) in text


def test_a_fault_without_a_remedy_is_sent_back_and_no_fault_needs_none():
    assert any("at least one step" in p for p in guards.check_remedies(_with_remedy()))
    assert guards.check_remedies(_with_remedy(cause="no_fault")) == []


def test_a_remedy_labelled_below_its_floor_is_sent_back():
    problems = guards.check_remedies(_with_remedy(
        {"command": "kubectl drain node-1 --ignore-daemonsets", "risk": "medium"}))

    assert problems == [("hypothesis 1 remediate 1: 'kubectl drain node-1 "
                         "--ignore-daemonsets' is labelled medium, below its floor of high")]


def test_a_risky_step_must_say_how_to_undo_it_and_a_low_one_need_not():
    assert any("must say how to undo" in p for p in guards.check_remedies(
        _with_remedy({"action": "restart it", "risk": "medium", "undo": None})))
    assert guards.check_remedies(
        _with_remedy({"command": "kubectl uncordon n1", "risk": "low", "undo": None})) == []


def test_an_action_without_a_command_is_a_valid_step():
    """Often the honest answer: the fix is a change in git, a registry, or the code."""
    assert guards.check_remedies(_with_remedy(
        {"action": "Fix the image tag in the chart values and let Argo CD sync"})) == []


def test_a_remedy_off_the_allowlist_is_a_problem_not_a_rejection():
    problems = guards.check_remedies(
        _with_remedy({"command": "kubectl delete namespace oncall-lab"}))

    assert len(problems) == 1 and "delete may name only" in problems[0]


def test_a_credential_in_a_remedy_is_sent_back():
    """The engineer pastes it into a shell history and the journal would store it."""
    problems = guards.check_remedies(_with_remedy({
        "action": "Set the URL",
        "command": "kubectl set env deployment/api DB_PASSWORD=hunter2hunter2",
    }))

    assert any("credential" in p for p in problems)


def test_a_link_in_a_remedy_is_caught_by_the_prose_check():
    d = _with_remedy({"action": "Follow https://evil.example/fix"})

    assert any("remediate 1 action: contains a link" in p for p in guards.check_prose(d))


def test_a_remedy_aimed_outside_the_evidence_namespaces_is_sent_back():
    d = _with_remedy({"command": "kubectl rollout restart deployment web -n prod"})

    assert guards.check_remedy_namespaces(d, {"oncall-lab"}) != []
    assert guards.check_remedy_namespaces(d, {"prod"}) == []


# ---- placeholders (prompt v7) -------------------------------------------------------


@pytest.mark.parametrize(("command", "floor"), [
    ("kubectl set image deployment/badimage app=<registry>/<image>:<tag> -n oncall-lab",
     "medium"),
    ("kubectl delete pod <pod-name> -n oncall-lab", "low"),
    ("kubectl delete pods -l app=<app-label> -n oncall-lab", "high"),
])
def test_a_placeholder_is_a_value_to_fill_in_not_shell_redirection(command, floor):
    """v6 eval, 2026-10-29: four bundles lost a repair to <tag> read as '>'."""
    assert guards.check_remedy_command(command) == (None, floor)


@pytest.mark.parametrize("command", [
    "kubectl rollout restart deployment/web > out.txt",
    "kubectl set image deployment/web app=<Image> -n x",
    "kubectl delete pod x < input",
])
def test_redirection_and_anything_not_a_placeholder_is_still_refused(command):
    reason, _ = guards.check_remedy_command(command)

    assert reason is not None and "shell syntax" in reason


def test_a_placeholder_never_passes_in_a_verify_command():
    assert guards.check_command("kubectl describe pod <pod-name> -n oncall-lab") is not None


def test_a_placeholder_namespace_in_a_remedy_is_not_a_guessed_one():
    d = _with_remedy({"command": "kubectl delete pod <pod-name> -n <namespace>",
                      "risk": "low", "undo": None})

    assert guards.check_remedy_namespaces(d, {"oncall-lab"}) == []
    assert guards.check_remedies(d) == []


@pytest.mark.parametrize("command", [
    "kubectl describe node oncall-worker2",
    "kubectl get pods -n oncall-lab -o wide",
    "kubectl rollout status deployment/<name> -n oncall-lab",
])
def test_a_read_only_step_is_allowed_at_low(command):
    """v9 eval: five of six remedy repairs were read-only checks offered as steps."""
    assert guards.check_remedy_command(command) == (None, "low")


def test_a_read_only_step_still_may_not_print_a_secret():
    reason, _ = guards.check_remedy_command("kubectl get secret db -n oncall-lab -o yaml")

    assert reason is not None and "Secret" in reason
