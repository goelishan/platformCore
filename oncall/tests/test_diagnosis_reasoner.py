"""
The reasoner: every path through diagnose() ends in a record, and the record is honest.

  - Bedrock is replaced by a client that returns real-shaped Converse responses, or raises
    real ClientErrors, and keeps every request. The repair loop is judged by what was
    actually sent back to the model, not by a flag the reasoner sets on itself.
  - Bundles come from real rows (helpers in test_bundle), so labels and fingerprints
    are the ones a live run would produce.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from oncall import config
from oncall.diagnosis import reasoner
from oncall.diagnosis.schema import Outcome
from oncall.envelope import Severity, SignalKind, SignalSource
from oncall.evidence import bundle
from oncall.tests import test_bundle as tb

MODEL = "us.anthropic.claude-sonnet-4-6"


class FakeBedrock:
    """Diagnosis replies in order; remedy calls (prompt v9) answered from their own queue,
    or with one valid step per rank when the case is not about remedies. The two request
    lists are kept apart so a test counts the calls it is about."""

    def __init__(self, *replies, remedies=()):
        self.replies = list(replies)
        self.remedy_replies = list(remedies)
        self.requests: list[dict] = []
        self.remedy_requests: list[dict] = []

    def converse(self, **request):
        if request["toolConfig"]["toolChoice"]["tool"]["name"] == "record_remedies":
            self.remedy_requests.append(request)
            reply = self.remedy_replies.pop(0) if self.remedy_replies else remedy_answer()
        else:
            self.requests.append(request)
            reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def answer(payload: dict | None, stop: str = "tool_use", tokens=(1000, 200),
           tool: str = "record_diagnosis") -> dict:
    content = [{"toolUse": {"toolUseId": "tu-1", "name": tool,
                            "input": payload}}] if payload is not None else [{"text": "…"}]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
        "ResponseMetadata": {"RequestId": "req-1"},
    }


def throttled() -> ClientError:
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                       "Converse")


REMEDY = {"action": "Point the Deployment at an image tag that exists in its registry",
          "command": "kubectl rollout undo deployment/app", "risk": "medium",
          "undo": "kubectl rollout undo deployment/app again returns to the new revision"}


def remedy_answer(*steps_by_rank, tokens=(500, 100)) -> dict:
    """steps_by_rank[i] is rank i+1's list of steps; none given means REMEDY for ranks 1-3."""
    lists = steps_by_rank or ([REMEDY], [REMEDY], [REMEDY])
    return answer({"remedies": [{"rank": i, "remediate": list(steps)}
                                for i, steps in enumerate(lists, 1)]},
                  tokens=tokens, tool="record_remedies")


def diagnosis(supporting=("F1",), command="kubectl describe pod x", rank=1) -> dict:
    return {
        "summary": "Image cannot be pulled.",
        "insufficient_evidence": False,
        "hypotheses": [{
            "rank": rank, "cause_class": "image_pull", "title": "Pull fails",
            "mechanism": "registry does not resolve", "confidence": "high",
            "supporting": list(supporting), "contradicting": [],
            "verify": [{"command": command, "purpose": "events"}],
        }],
    }


@pytest.fixture
def lab(buffer):
    """Two findings: F1 is the error, F2 the info."""
    tb.store([
        tb.make_signal(when=tb.NOW - timedelta(minutes=2), key="app|Pulled|",
                       severity=Severity.INFO, payload={"reason": "Pulled"}),
        tb.make_signal(when=tb.NOW - timedelta(minutes=8), key="app|ErrImagePull|",
                       severity=Severity.ERROR, payload={"reason": "ErrImagePull"}),
    ])
    return tb.build()


def run(bundle, client):
    return reasoner.diagnose(bundle, incident_id="inc-1", client=client, model_id=MODEL)


def test_a_valid_answer_is_kept_with_citations_as_fingerprints(lab, monkeypatch):
    monkeypatch.setattr(config, "PRICE_INPUT_PER_MTOK", 3.0)
    monkeypatch.setattr(config, "PRICE_OUTPUT_PER_MTOK", 15.0)

    r = run(lab, FakeBedrock(answer(diagnosis(), tokens=(8420, 1192)),
                             remedies=[remedy_answer(tokens=(0, 0))]))

    assert r.outcome is Outcome.OK
    assert r.attempts == 1
    assert r.diagnosis.hypotheses[0].supporting == [lab.findings[0].fingerprint]
    assert (r.input_tokens, r.output_tokens) == (8420, 1192)
    assert r.cost_usd == pytest.approx(8420 / 1e6 * 3.0 + 1192 / 1e6 * 15.0)
    assert r.model == MODEL
    assert r.errors == []


def test_cost_is_absent_rather_than_zero_without_prices(lab, monkeypatch):
    monkeypatch.setattr(config, "PRICE_INPUT_PER_MTOK", None)

    assert run(lab, FakeBedrock(answer(diagnosis()))).cost_usd is None


def test_a_malformed_answer_is_repaired_once_with_the_specific_errors(lab):
    client = FakeBedrock(answer(diagnosis(rank=2)), answer(diagnosis(), tokens=(1100, 150)),
                         remedies=[remedy_answer(tokens=(0, 0))])

    r = run(lab, client)

    assert r.outcome is Outcome.OK
    assert r.attempts == 2
    assert (r.input_tokens, r.output_tokens) == (2100, 350)

    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    result = second[2]["content"][0]["toolResult"]
    assert result["toolUseId"] == "tu-1"
    assert result["status"] == "error"
    assert "ranks must run" in result["content"][0]["text"]


def test_malformed_twice_is_invalid_output_and_keeps_no_diagnosis(lab):
    r = run(lab, FakeBedrock(answer({"summary": "x"}), answer({"summary": "y"})))

    assert r.outcome is Outcome.INVALID_OUTPUT
    assert r.attempts == 2
    assert r.diagnosis is None
    assert any(e.startswith("attempt 1:") for e in r.errors)
    assert any(e.startswith("attempt 2:") for e in r.errors)


def test_a_citation_outside_the_bundle_is_sent_back_for_repair(lab):
    client = FakeBedrock(answer(diagnosis(supporting=["F1", "F9"])), answer(diagnosis()))

    r = run(lab, client)

    assert r.outcome is Outcome.OK
    assert r.attempts == 2
    text = client.requests[1]["messages"][2]["content"][0]["toolResult"]["content"][0]["text"]
    assert "F9 is not a finding ID in this bundle" in text


@pytest.mark.parametrize(
    "command",
    ["kubectl delete deployment stuckrollout", "kubectl exec -it x -- sh",
     "kubectl get pods | sh"],
)
def test_an_unsafe_command_is_rejected_without_a_retry(lab, command):
    """Only one request may be made. A retry would invite the model to hide the write it
    proposed, and the proposal is itself the finding."""
    client = FakeBedrock(answer(diagnosis(command=command)), answer(diagnosis()))

    r = run(lab, client)

    assert r.outcome is Outcome.GUARD_REJECTED
    assert r.diagnosis is None
    assert len(client.requests) == 1
    assert command in r.errors[0]


def test_an_unsafe_command_wins_over_a_bad_citation(lab):
    """Checked first: a diagnosis that is both wrong about its evidence and unsafe is
    rejected for the unsafe part, not repaired into a safer-looking one."""
    bad = diagnosis(supporting=["F9"], command="kubectl delete pod x")

    r = run(lab, FakeBedrock(answer(bad), answer(diagnosis())))

    assert r.outcome is Outcome.GUARD_REJECTED
    assert r.attempts == 1


def test_running_out_of_tokens_is_recorded_not_retried(lab):
    client = FakeBedrock(answer(None, stop="max_tokens"), answer(diagnosis()))

    r = run(lab, client)

    assert r.outcome is Outcome.INVALID_OUTPUT
    assert r.stop_reason == "max_tokens"
    assert len(client.requests) == 1
    assert "stop_reason max_tokens" in r.errors[0]


def test_a_truncated_but_present_tool_call_is_not_retried_either(lab):
    """The tool block can arrive with its arguments cut short. The same budget would cut
    the same answer, so this is final even though there is a toolUse to answer."""
    client = FakeBedrock(answer({"summary": "cut"}, stop="max_tokens"), answer(diagnosis()))

    r = run(lab, client)

    assert r.outcome is Outcome.INVALID_OUTPUT
    assert len(client.requests) == 1


def test_throttling_is_a_model_error_with_its_class(lab):
    r = run(lab, FakeBedrock(throttled()))

    assert r.outcome is Outcome.MODEL_ERROR
    assert r.attempts == 1
    assert "throttled: ThrottlingException" in r.errors[0]
    assert r.diagnosis is None


def test_an_error_on_the_repair_attempt_keeps_the_first_attempts_cost(lab):
    r = run(lab, FakeBedrock(answer(diagnosis(rank=2), tokens=(900, 100)), throttled()))

    assert r.outcome is Outcome.MODEL_ERROR
    assert r.attempts == 2
    assert (r.input_tokens, r.output_tokens) == (900, 100)


def test_the_record_carries_both_receipts_and_the_prompt_version(lab):
    from oncall.diagnosis import prompt
    from oncall.evidence import bundle

    r = run(lab, FakeBedrock(answer(diagnosis())))

    assert r.bundle_sha256 == bundle.evidence_receipt(lab)
    assert r.prompt_sha256 == prompt.build(lab).prompt_sha256
    assert r.prompt_version == prompt.PROMPT_VERSION


def test_the_reasoner_cannot_reach_the_cluster():
    """The diagnose-only boundary as a test rather than a promise: importing the
    reasoner must not load the Kubernetes client at all."""
    code = (
        "import sys, oncall.diagnosis.reasoner\n"
        "bad = sorted(m for m in sys.modules if m == 'kubernetes' or m.startswith('kubernetes.'))\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0, f"kubernetes modules loaded: {done.stdout or done.stderr}"


def test_a_link_in_the_prose_is_sent_back_for_repair(lab):
    linked = diagnosis()
    linked["summary"] = "Image cannot be pulled; details at https://evil.example/fix"
    client = FakeBedrock(answer(linked), answer(diagnosis()))

    r = run(lab, client)

    assert r.outcome is Outcome.OK
    assert r.attempts == 2
    feedback = client.requests[1]["messages"][2]["content"][0]["toolResult"]
    assert "summary: contains a link" in feedback["content"][0]["text"]


def test_a_command_aimed_at_a_namespace_outside_the_evidence_is_sent_back(lab):
    """F4: the guard is wired into the repair loop, not only defined."""
    present = sorted(reasoner.evidence_namespaces(lab))
    client = FakeBedrock(answer(diagnosis(command="kubectl get pods -n loki")),
                         answer(diagnosis(command=f"kubectl get pods -n {present[0]}")))

    r = run(lab, client)

    assert r.outcome is Outcome.OK
    assert r.attempts == 2
    feedback = client.requests[1]["messages"][2]["content"][0]["toolResult"]
    assert "namespace loki appears nowhere" in feedback["content"][0]["text"]


def test_remedies_come_from_a_second_call_that_cannot_rank(lab):
    """Prompt v9. Offering remedies in the diagnosis call moved the leading cause on the
    unschedulable fixture (3/3 against 0/3). The diagnosis call is offered no remediate
    field and no remedy rules; the remedy call gets the finished hypotheses as data."""
    client = FakeBedrock(answer(diagnosis()))

    r = run(lab, client)

    (diag,), (fix,) = client.requests, client.remedy_requests
    diag_tool = diag["toolConfig"]["tools"][0]["toolSpec"]
    assert "remediate" not in json.dumps(diag_tool["inputSchema"])
    assert "remediate" not in diag["system"][0]["text"]
    assert "<diagnosis>" in fix["messages"][0]["content"][0]["text"]
    assert "Pull fails" in fix["messages"][0]["content"][0]["text"]
    assert r.outcome is Outcome.OK and r.attempts == 1
    assert r.diagnosis.hypotheses[0].remediate[0].risk == "medium"


def test_both_calls_are_paid_for_and_journalled_as_one(lab, monkeypatch):
    monkeypatch.setattr(config, "PRICE_INPUT_PER_MTOK", 3.0)
    monkeypatch.setattr(config, "PRICE_OUTPUT_PER_MTOK", 15.0)

    r = run(lab, FakeBedrock(answer(diagnosis(), tokens=(1000, 200)),
                             remedies=[remedy_answer(tokens=(600, 50))]))

    assert (r.input_tokens, r.output_tokens) == (1600, 250)
    assert r.cost_usd == pytest.approx(1600 / 1e6 * 3 + 250 / 1e6 * 15)


def test_a_fault_without_remedy_steps_is_repaired_in_the_remedy_call(lab):
    client = FakeBedrock(answer(diagnosis()), remedies=[remedy_answer([]), remedy_answer()])

    r = run(lab, client)

    assert r.outcome is Outcome.OK
    assert r.attempts == 1, "the diagnosis was not asked again"
    assert len(client.remedy_requests) == 2
    feedback = client.remedy_requests[1]["messages"][2]["content"][0]["toolResult"]
    assert "remediate must give at least one step" in feedback["content"][0]["text"]
    assert r.diagnosis.hypotheses[0].remediate[0].risk == "medium"


@pytest.mark.parametrize(("step", "error"), [
    ({"action": "Recreate the namespace", "risk": "high", "undo": "none",
      "command": "kubectl delete namespace oncall-lab"}, "delete may name only"),
    ({"action": "Drain the node", "risk": "low",
      "command": "kubectl drain node-1 --ignore-daemonsets"}, "below its floor of high"),
    ({"action": "Roll back", "risk": "medium", "undo": "roll forward",
      "command": "kubectl rollout undo deployment/app -n staging"}, "namespace staging"),
    ({"action": "See https://evil.example/fix", "risk": "low"}, "contains a link"),
])
def test_a_bad_step_is_repaired_and_never_costs_the_diagnosis(lab, step, error):
    client = FakeBedrock(answer(diagnosis()), remedies=[remedy_answer([step]), remedy_answer()])

    r = run(lab, client)

    assert r.outcome is Outcome.OK
    assert any(e.startswith("remedies attempt 1") and error in e for e in r.errors)
    assert r.diagnosis.hypotheses[0].remediate[0].command == REMEDY["command"]


def test_remedies_that_fail_twice_leave_the_diagnosis_standing_without_steps(lab):
    bad = {"action": "Drain", "risk": "low", "command": "kubectl drain n"}
    client = FakeBedrock(answer(diagnosis()),
                         remedies=[remedy_answer([bad]), remedy_answer([bad])])

    r = run(lab, client)

    assert r.outcome is Outcome.OK
    assert r.diagnosis.hypotheses[0].remediate == []
    assert sum(e.startswith("remedies attempt") for e in r.errors) == 2


def test_a_model_error_on_the_remedy_call_keeps_the_diagnosis(lab):
    r = run(lab, FakeBedrock(answer(diagnosis()), remedies=[throttled()]))

    assert r.outcome is Outcome.OK
    assert r.diagnosis.hypotheses[0].remediate == []
    assert any("remedies attempt 1: throttled" in e for e in r.errors)


def test_a_failed_diagnosis_makes_no_remedy_call(lab):
    client = FakeBedrock(answer(diagnosis(command="kubectl delete pod x")))

    run(lab, client)

    assert client.remedy_requests == []


def test_an_unsafe_verify_is_still_rejected_outright_beside_a_good_remedy(lab):
    client = FakeBedrock(answer(diagnosis(command="kubectl delete pod x")))

    r = run(lab, client)

    assert r.outcome is Outcome.GUARD_REJECTED
    assert r.attempts == 1


@pytest.fixture
def neighbour(buffer):
    """F1 is the subject's own error; a Service in the same namespace is admitted only
    via namespace and, at error, ranks beside it."""
    tb.store([tb.make_signal(when=tb.NOW - timedelta(minutes=8), key="app|ErrImagePull|",
                             severity=Severity.ERROR, payload={"reason": "ErrImagePull"})])
    tb.store([tb.make_signal(when=tb.NOW - timedelta(minutes=8), key="NoEndpoints",
                             severity=Severity.ERROR, pod="web-svc",
                             source=SignalSource.K8S_SERVICES, kind=SignalKind.POD_STATE,
                             payload={"reason": "NoEndpoints"})],
             source=SignalSource.K8S_SERVICES)
    return tb.build()


def test_a_leading_hypothesis_on_a_namespace_neighbour_is_sent_back(neighbour):
    labels = bundle.finding_labels(neighbour)
    by_join = {f.joined_by: labels[f.fingerprint] for f in neighbour.findings}
    client = FakeBedrock(answer(diagnosis(supporting=[by_join["namespace"]])),
                         answer(diagnosis(supporting=[by_join["subject"]])))

    r = run(neighbour, client)

    assert r.outcome is Outcome.OK
    assert r.attempts == 2
    assert any("only via namespace" in e for e in r.errors)


def test_evidence_over_the_cap_is_journalled_and_nothing_is_sent(lab, monkeypatch):
    """A bundle that skipped fit(), a frozen fixture or one replayed under a smaller
    cap. Raising would leave no record; the metrics need one."""
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 100)
    client = FakeBedrock()

    r = run(lab, client)

    assert r.outcome is Outcome.PROMPT_TOO_LARGE
    assert client.requests == []
    assert (r.attempts, r.input_tokens, r.prompt_sha256, r.diagnosis) == (0, 0, None, None)
    assert r.bundle_sha256 == bundle.evidence_receipt(lab)
    assert "over the prompt cap of 100" in r.errors[0]
