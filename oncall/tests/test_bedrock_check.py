"""
The Bedrock preflight, against fake AWS clients.

  - Each check is fed the response that decides it, so the test states which fact turns
    a PASS into a FAIL. The real clients are only built in main(), which is not tested.
  - The routing case that matters most is the one that works on a laptop and fails in
    the cluster: a profile that routes to a model ARN the IAM policy does not name.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from oncall import config
from oncall.dev import bedrock_check as bc

PROFILE = "us.anthropic.claude-sonnet-4-6"
GRANTED = [f"arn:aws:bedrock:{r}::foundation-model/anthropic.claude-sonnet-4-6"
           for r in ("us-east-1", "us-east-2", "us-west-2")]


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", PROFILE)
    monkeypatch.setattr(config, "BEDROCK_REGION", "us-east-1")
    monkeypatch.setattr(config, "BEDROCK_GUARDRAIL_ID", None)


def denied(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, op)


class Sts:
    def __init__(self, arn="arn:aws:sts::111:assumed-role/pc-oncall-agent/botocore-1"):
        self.arn = arn

    def get_caller_identity(self):
        return {"Account": "111", "Arn": self.arn}


class Bedrock:
    def __init__(self, models=GRANTED, error=None, status="ACTIVE"):
        self.models, self.error, self.status = models, error, status

    def get_inference_profile(self, inferenceProfileIdentifier):
        if self.error:
            raise self.error
        return {"status": self.status, "models": [{"modelArn": m} for m in self.models]}


class Runtime:
    def __init__(self, error=None):
        self.error, self.requests = error, []

    def converse(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return {"usage": {"inputTokens": 12, "outputTokens": 2},
                "metrics": {"latencyMs": 410}, "ResponseMetadata": {"RequestId": "r-1"}}


def resolver(*addresses):
    return lambda host, port: [(None, None, None, "", (a, port)) for a in addresses]


def test_a_bare_model_id_fails_before_any_call(monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6")
    runtime = Runtime()

    results = bc.run(Sts(), Bedrock(), runtime, resolver("10.0.1.5"))

    assert [(r.name, r.status) for r in results] == [("config", bc.FAIL)]
    assert runtime.requests == []


def test_a_us_profile_called_from_another_geography_fails(monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_REGION", "ap-south-1")

    assert bc.check_config().status == bc.FAIL


def test_the_irsa_role_is_named_as_such():
    assert "(IRSA role)" in bc.check_identity(Sts()).detail
    assert "(not IRSA)" in bc.check_identity(Sts("arn:aws:iam::111:user/tf")).detail


def test_a_private_endpoint_passes_and_a_mixed_one_fails():
    assert bc.check_network("us-east-1", resolver("10.0.1.5", "10.0.2.7")).status == bc.PASS
    assert bc.check_network("us-east-1", resolver("10.0.1.5", "3.5.1.2")).status == bc.FAIL


def test_a_public_endpoint_fails_only_inside_the_cluster(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert bc.check_network("us-east-1", resolver("3.5.1.2")).status == bc.PASS

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    result = bc.check_network("us-east-1", resolver("3.5.1.2"))
    assert result.status == bc.FAIL
    assert "NAT" in result.detail


def test_routing_to_a_model_the_policy_does_not_grant_fails():
    extra = GRANTED + ["arn:aws:bedrock:us-west-1::foundation-model/anthropic.claude-sonnet-4-6"]

    result = bc.check_routing(Bedrock(models=extra))

    assert result.status == bc.FAIL
    assert "us-west-1" in result.detail


def test_routing_inside_the_grant_passes_and_denied_reads_skip():
    assert bc.check_routing(Bedrock()).status == bc.PASS
    assert bc.check_routing(Bedrock(error=denied("GetInferenceProfile"))).status == bc.SKIP
    assert bc.check_routing(Bedrock(status="DISABLED")).status == bc.FAIL


def test_the_call_uses_the_profile_and_any_guardrail(monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_GUARDRAIL_ID", "gr-1")
    runtime = Runtime()

    result = bc.check_invoke(runtime)

    assert result.status == bc.PASS
    assert runtime.requests[0]["modelId"] == PROFILE
    assert runtime.requests[0]["guardrailConfig"]["guardrailIdentifier"] == "gr-1"
    assert "request r-1" in result.detail


def test_a_denied_call_fails_with_its_code():
    result = bc.check_invoke(Runtime(error=denied("Converse")))

    assert result.status == bc.FAIL
    assert result.detail.startswith("AccessDeniedException")


def test_everything_runs_when_identity_works():
    results = bc.run(Sts(), Bedrock(), Runtime(), resolver("10.0.1.5"))

    assert [r.name for r in results] == ["config", "identity", "network", "routing", "invoke"]
    assert {r.status for r in results} == {bc.PASS}
