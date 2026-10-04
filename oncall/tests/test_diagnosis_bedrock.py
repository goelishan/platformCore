"""
The Converse client, against botocore's Stubber on a real bedrock-runtime client.

  - Stubber validates every request against the service's own API model, so a
    malformed toolConfig or a wrong key fails here rather than on a paid call. It
    validates the canned responses too, so they cannot drift from what Bedrock sends.
  - Retries are switched off in the fixture. The production client retries throttling
    itself; here each stubbed error must surface once, as the error it is.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber

from oncall import config
from oncall.diagnosis import bedrock, prompt

MODEL = "us.anthropic.claude-sonnet-4-6"

PROMPT = prompt.Prompt(
    system="system text",
    user="<evidence>\nF1 ...\n</evidence>",
    tool=prompt.tool_definition(),
    labels={"F1": "a" * 32},
    version="1",
    bundle_sha256="b" * 64,
    prompt_sha256="p" * 64,
)

TOOL_INPUT = {"summary": "s", "hypotheses": [], "insufficient_evidence": True,
              "unknowns": ["x"]}


@pytest.fixture
def client():
    c = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(retries={"total_max_attempts": 1}),
    )
    with Stubber(c) as stub:
        c.stub = stub
        yield c
        stub.assert_no_pending_responses()


def response(content: list, stop: str = "tool_use") -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": {"inputTokens": 5200, "outputTokens": 410, "totalTokens": 5610},
        "metrics": {"latencyMs": 3100},
    }


def tool_use(name: str = "record_diagnosis", payload: dict | None = None) -> dict:
    return {"toolUse": {"toolUseId": "tu-1", "name": name, "input": payload or TOOL_INPUT}}


def expected(**overrides) -> dict:
    params = {
        "modelId": MODEL,
        "system": [{"text": "system text"}],
        "messages": [{"role": "user", "content": [{"text": PROMPT.user}]}],
        "inferenceConfig": {"maxTokens": 2000, "temperature": 0},
        "toolConfig": {
            "tools": [{"toolSpec": prompt.tool_definition()}],
            "toolChoice": {"tool": {"name": "record_diagnosis"}},
        },
    }
    params.update(overrides)
    return params


def test_the_request_forces_the_tool_and_pins_temperature(client):
    """expected_params makes Stubber compare the whole request, so a missing
    toolChoice or a drifted temperature fails here, not only a malformed one."""
    client.stub.add_response("converse", response([tool_use()]), expected())

    bedrock.converse(PROMPT, client=client, model_id=MODEL, max_tokens=2000)


def test_a_tool_reply_is_read_with_its_usage_and_turn(client):
    client.stub.add_response("converse", response([{"text": "thinking"}, tool_use()]))

    reply = bedrock.converse(PROMPT, client=client, model_id=MODEL)

    assert reply.tool_input == TOOL_INPUT
    assert reply.stop_reason == "tool_use"
    assert (reply.input_tokens, reply.output_tokens) == (5200, 410)
    assert reply.tool_use_id == "tu-1"
    assert reply.message["content"][1]["toolUse"]["toolUseId"] == "tu-1"
    assert reply.latency_ms >= 0


def test_a_reply_cut_off_before_the_tool_has_no_tool_input(client):
    """max_tokens on a forced tool call means the model ran out of room before or while
    writing the arguments. There is nothing to validate, and the stop reason is the
    explanation the reasoner journals."""
    client.stub.add_response("converse", response([{"text": "partial"}], "max_tokens"))

    reply = bedrock.converse(PROMPT, client=client, model_id=MODEL)

    assert reply.tool_input is None
    assert reply.tool_use_id is None
    assert reply.stop_reason == "max_tokens"


def test_a_tool_of_another_name_is_not_taken_as_the_answer(client):
    client.stub.add_response("converse", response([tool_use(name="run_shell")]))

    assert bedrock.converse(PROMPT, client=client, model_id=MODEL).tool_input is None


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("ThrottlingException", bedrock.THROTTLED),
        ("ModelTimeoutException", bedrock.TIMEOUT),
        ("ValidationException", bedrock.INVALID_REQUEST),
        ("AccessDeniedException", bedrock.ACCESS_DENIED),
        ("ResourceNotFoundException", bedrock.ACCESS_DENIED),
        ("ServiceUnavailableException", bedrock.UNAVAILABLE),
        ("SomethingNewException", bedrock.OTHER),
    ],
)
def test_errors_are_classified_by_what_a_caller_can_do(client, code, kind):
    client.stub.add_client_error("converse", service_error_code=code,
                                 service_message="boom", http_status_code=400)

    with pytest.raises(bedrock.ModelError) as raised:
        bedrock.converse(PROMPT, client=client, model_id=MODEL)

    assert raised.value.kind == kind
    assert raised.value.code == code


def test_a_read_timeout_is_a_timeout():
    class Slow:
        def converse(self, **_):
            raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1")

    with pytest.raises(bedrock.ModelError) as raised:
        bedrock.converse(PROMPT, client=Slow(), model_id=MODEL)

    assert raised.value.kind == bedrock.TIMEOUT


def test_no_configured_model_is_refused_before_any_call(monkeypatch):
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", None)

    with pytest.raises(bedrock.ModelError, match="ONCALL_BEDROCK_MODEL_ID"):
        bedrock.converse(PROMPT, client=object())


def test_a_guardrail_is_attached_only_when_configured(client, monkeypatch):
    client.stub.add_response("converse", response([tool_use()]), expected())
    bedrock.converse(PROMPT, client=client, model_id=MODEL, max_tokens=2000)

    monkeypatch.setattr(config, "BEDROCK_GUARDRAIL_ID", "gr-1")
    monkeypatch.setattr(config, "BEDROCK_GUARDRAIL_VERSION", "3")
    client.stub.add_response(
        "converse",
        response([tool_use()]),
        expected(guardrailConfig={"guardrailIdentifier": "gr-1", "guardrailVersion": "3"}),
    )
    bedrock.converse(PROMPT, client=client, model_id=MODEL, max_tokens=2000)


def test_a_repair_turn_is_sent_as_given(client):
    """The reasoner builds the follow-up conversation; this layer must send it
    unchanged rather than starting over from the first message."""
    turns = [
        {"role": "user", "content": [{"text": PROMPT.user}]},
        {"role": "assistant", "content": [tool_use()]},
        {"role": "user", "content": [{"toolResult": {
            "toolUseId": "tu-1", "status": "error", "content": [{"text": "ranks must run"}],
        }}]},
    ]
    client.stub.add_response("converse", response([tool_use()]), expected(messages=turns))

    bedrock.converse(PROMPT, turns, client=client, model_id=MODEL, max_tokens=2000)
