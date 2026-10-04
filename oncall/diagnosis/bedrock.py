"""
A thin Converse client: one request shape, one reply shape, errors sorted into kinds.

  - The model answers only through the record_diagnosis tool. toolChoice names the tool,
    so a prose reply is not an option the model is offered; a reply without a toolUse
    block means the call ended early, and stop_reason says why.
  - Retries are botocore's, in adaptive mode, which backs off on throttling with a
    client-side rate limiter. This layer only classifies what is left after them, so a
    caller can tell an exhausted quota from a missing permission without parsing text.
  - One client per process. Each new client pays DNS, TCP and TLS before its first
    request, about 800 ms from a laptop to us-east-1 on 2026-10-04; a reused one pays
    it once.
  - Nothing here reads the cluster, the buffer or the store. The model sees the prompt
    it is handed and nothing else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from oncall import config
from oncall.diagnosis.prompt import Prompt

# Bedrock error codes by what a caller can do about them. Anything unlisted is OTHER,
# and its code is kept, so a new code shows up in the journal rather than being lost.
THROTTLED = "throttled"
TIMEOUT = "timeout"
INVALID_REQUEST = "invalid_request"
ACCESS_DENIED = "access_denied"
UNAVAILABLE = "unavailable"
OTHER = "other"

_KINDS = {
    "ThrottlingException": THROTTLED,
    "ServiceQuotaExceededException": THROTTLED,
    "ModelTimeoutException": TIMEOUT,
    "ValidationException": INVALID_REQUEST,
    "AccessDeniedException": ACCESS_DENIED,
    # Also what Bedrock returns while the account's Anthropic use-case form is missing,
    # seen live on 2026-10-04. Not retryable either way.
    "ResourceNotFoundException": ACCESS_DENIED,
    "ServiceUnavailableException": UNAVAILABLE,
    "ModelNotReadyException": UNAVAILABLE,
    "InternalServerException": UNAVAILABLE,
    "ModelErrorException": UNAVAILABLE,
}


class ModelError(Exception):
    def __init__(self, kind: str, code: str, message: str, request_id: str | None = None):
        super().__init__(f"{kind}: {code}: {message}")
        self.kind = kind
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class Reply:
    tool_input: dict[str, Any] | None
    stop_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    request_id: str | None
    # The assistant turn as Bedrock returned it. A repair attempt has to send it back,
    # because Converse requires a toolResult to answer the toolUse that preceded it.
    message: dict[str, Any]
    tool_use_id: str | None


@cache
def default_client() -> Any:
    return boto3.client(
        "bedrock-runtime",
        region_name=config.BEDROCK_REGION,
        config=Config(
            connect_timeout=config.BEDROCK_CONNECT_TIMEOUT,
            read_timeout=config.BEDROCK_READ_TIMEOUT,
            # total_max_attempts counts the first request; botocore's max_attempts
            # counts only retries, so the same 3 there would allow four calls.
            retries={"mode": "adaptive", "total_max_attempts": config.BEDROCK_MAX_ATTEMPTS},
        ),
    )


def request_for(
    prompt: Prompt,
    messages: list[dict[str, Any]],
    model_id: str,
    max_tokens: int,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "modelId": model_id,
        "system": [{"text": prompt.system}],
        "messages": messages,
        # Temperature 0 narrows the spread of answers; it does not make them identical.
        # The eval measures how often a repeated run agrees with itself.
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
        "toolConfig": {
            "tools": [{"toolSpec": prompt.tool}],
            "toolChoice": {"tool": {"name": prompt.tool["name"]}},
        },
    }
    if config.BEDROCK_GUARDRAIL_ID:
        request["guardrailConfig"] = {
            "guardrailIdentifier": config.BEDROCK_GUARDRAIL_ID,
            "guardrailVersion": config.BEDROCK_GUARDRAIL_VERSION or "DRAFT",
        }
    return request


def first_message(prompt: Prompt) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"text": prompt.user}]}]


def converse(
    prompt: Prompt,
    messages: list[dict[str, Any]] | None = None,
    *,
    client: Any = None,
    model_id: str | None = None,
    max_tokens: int | None = None,
) -> Reply:
    model = model_id or config.BEDROCK_MODEL_ID
    if not model:
        raise ModelError(INVALID_REQUEST, "NoModelConfigured", "ONCALL_BEDROCK_MODEL_ID is unset")

    request = request_for(
        prompt,
        messages or first_message(prompt),
        model,
        max_tokens or config.BEDROCK_MAX_TOKENS,
    )
    started = time.monotonic()
    try:
        response = (client or default_client()).converse(**request)
    except ClientError as e:
        error = e.response.get("Error", {})
        code = error.get("Code", "Unknown")
        raise ModelError(
            _KINDS.get(code, OTHER),
            code,
            error.get("Message", ""),
            e.response.get("ResponseMetadata", {}).get("RequestId"),
        ) from e
    except (ReadTimeoutError, ConnectTimeoutError) as e:
        raise ModelError(TIMEOUT, type(e).__name__, str(e)) from e
    except EndpointConnectionError as e:
        raise ModelError(UNAVAILABLE, type(e).__name__, str(e)) from e
    latency_ms = int((time.monotonic() - started) * 1000)

    message = response["output"]["message"]
    tool_use = next(
        (
            block["toolUse"]
            for block in message.get("content", [])
            if "toolUse" in block and block["toolUse"].get("name") == prompt.tool["name"]
        ),
        None,
    )
    usage = response.get("usage", {})
    return Reply(
        tool_input=tool_use["input"] if tool_use else None,
        stop_reason=response["stopReason"],
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        latency_ms=latency_ms,
        request_id=response.get("ResponseMetadata", {}).get("RequestId"),
        message=message,
        tool_use_id=tool_use["toolUseId"] if tool_use else None,
    )
