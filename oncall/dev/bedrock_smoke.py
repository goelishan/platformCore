"""
Bedrock smoke test.

  - One Converse call through the configured inference profile

  - Prints the reply, stop reason, token usage, latency and request ID
"""

from __future__ import annotations

import time

import boto3
from botocore.config import Config

from oncall import config


if not config.BEDROCK_REGION or not config.BEDROCK_MODEL_ID:
    raise SystemExit("set ONCALL_BEDROCK_REGION and ONCALL_BEDROCK_MODEL_ID in oncall/.env")

client = boto3.client(
    "bedrock-runtime",
    region_name=config.BEDROCK_REGION,
    config=Config(
        connect_timeout=config.BEDROCK_CONNECT_TIMEOUT,
        read_timeout=config.BEDROCK_READ_TIMEOUT,
        retries={"mode": "adaptive", "max_attempts": config.BEDROCK_MAX_ATTEMPTS},
    ),
)

started = time.monotonic()
response = client.converse(
    modelId=config.BEDROCK_MODEL_ID,
    messages=[{"role": "user", "content": [{"text": "Reply with OK and nothing else."}]}],
    inferenceConfig={"maxTokens": 10, "temperature": 0},
)
elapsed_ms = int((time.monotonic() - started) * 1000)

print("reply      ", response["output"]["message"]["content"][0]["text"])
print("stopReason ", response["stopReason"])
print("usage      ", response["usage"])
print("latency_ms ", elapsed_ms, "client,", response["metrics"]["latencyMs"], "server")
print("request_id ", response["ResponseMetadata"]["RequestId"])
