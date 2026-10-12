"""
Bedrock preflight: is this process linked to the model the way the deploy assumes?

  - Five checks, in the order a failure is easiest to explain: configuration, identity,
    network path, inference-profile routing, one real call. Each prints PASS, FAIL or
    SKIP with the one fact that decided it, and the exit code is non-zero on any FAIL.
  - Routing is compared with what the IAM policy grants. Terraform names one profile
    ARN and a foundation-model ARN per region; if the profile routes to a model or a
    region the policy does not name, every call from the cluster is denied while every
    call from a laptop with broader credentials works. This check is where that shows.
  - The network check resolves the runtime endpoint. A private address means the VPC
    interface endpoint is in the path; a public one from inside the cluster means
    prompts leave through the NAT gateway, which the deploy notes forbid.
  - The call is one Converse request through the reasoner's own client, so retries,
    timeouts and any guardrail are exactly what a diagnosis gets. About 20 input
    tokens: a fraction of a cent.
  - Read-only apart from that call. Permission to read the inference profile is not
    something every role has; without it the routing check is a SKIP, not a FAIL.

    make -C oncall bedrock-check
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from oncall import config

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# Geography prefixes of cross-region inference profiles, and the region families each
# routes inside. A us. profile called from eu-west-1 works, but its traffic crosses
# geographies and the VPC endpoint in the calling region is not the one it reaches.
PROFILE_GEOGRAPHY = {"us.": "us-", "eu.": "eu-", "apac.": "ap-", "in.": "ap-south-"}

# What Terraform grants, as variables of the same name. Defaults mirror
# terraform/modules/eks/variable.tf so a laptop run checks the shipped policy.
EXPECTED_FOUNDATION_MODEL = os.getenv(
    "ONCALL_BEDROCK_FOUNDATION_MODEL", "anthropic.claude-sonnet-4-6"
)
EXPECTED_MODEL_REGIONS = tuple(
    r for r in os.getenv(
        "ONCALL_BEDROCK_MODEL_REGIONS", "us-east-1,us-east-2,us-west-2"
    ).split(",") if r
)


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    detail: str


def check_config() -> Result:
    model, region = config.BEDROCK_MODEL_ID, config.BEDROCK_REGION
    if not model or not region:
        return Result("config", FAIL, "ONCALL_BEDROCK_MODEL_ID and _REGION must both be set")
    prefix = next((p for p in PROFILE_GEOGRAPHY if model.startswith(p)), None)
    if prefix is None and not model.startswith("global."):
        return Result("config", FAIL, f"{model} is not an inference profile; current "
                      "Claude models refuse a bare model ID")
    if prefix and not region.startswith(PROFILE_GEOGRAPHY[prefix]):
        return Result("config", FAIL, f"{prefix} profile called from {region}: the call "
                      "leaves its geography and misses this region's VPC endpoint")
    return Result("config", PASS, f"{model} from {region}")


def check_identity(sts: Any) -> Result:
    try:
        who = sts.get_caller_identity()
    except (ClientError, BotoCoreError) as e:
        return Result("identity", FAIL, f"no usable AWS credentials: {e}")
    arn = who["Arn"]
    # In the cluster this is the IRSA role; anything else means the pod is running on
    # the node's role or on static keys, and the least-privilege policy is not in play.
    via = "IRSA role" if ":assumed-role/" in arn and "oncall-agent" in arn else "not IRSA"
    return Result("identity", PASS, f"{arn} ({via})")


def check_network(region: str, resolve: Callable[..., list] = socket.getaddrinfo) -> Result:
    host = f"bedrock-runtime.{region}.amazonaws.com"
    try:
        addresses = sorted({info[4][0] for info in resolve(host, 443)})
    except OSError as e:
        return Result("network", FAIL, f"{host} does not resolve: {e}")
    private = [a for a in addresses if ipaddress.ip_address(a).is_private]
    if private and len(private) == len(addresses):
        return Result("network", PASS, f"{host} -> {', '.join(addresses)} (VPC endpoint)")
    if private:
        return Result("network", FAIL, f"{host} resolves to private and public "
                      f"addresses: {', '.join(addresses)}")
    # Public is right on a laptop and wrong in the cluster; the caller says which.
    in_cluster = bool(os.getenv("KUBERNETES_SERVICE_HOST"))
    status = FAIL if in_cluster else PASS
    where = "in-cluster, so prompts would use NAT" if in_cluster else "outside the cluster"
    return Result("network", status, f"{host} -> {', '.join(addresses)} (public, {where})")


def check_routing(bedrock: Any) -> Result:
    try:
        profile = bedrock.get_inference_profile(
            inferenceProfileIdentifier=config.BEDROCK_MODEL_ID
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "AccessDeniedException":
            return Result("routing", SKIP, "this role may not read inference profiles; "
                          "the policy check needs a run with broader credentials")
        return Result("routing", FAIL, f"{code}: profile not readable")
    except BotoCoreError as e:
        return Result("routing", FAIL, str(e))

    actual = sorted(m["modelArn"] for m in profile.get("models", []))
    expected = sorted(
        f"arn:aws:bedrock:{r}::foundation-model/{EXPECTED_FOUNDATION_MODEL}"
        for r in EXPECTED_MODEL_REGIONS
    )
    missing = [a for a in actual if a not in expected]
    if missing:
        return Result("routing", FAIL, "the profile routes to ARNs the IAM policy does not "
                      f"grant: {', '.join(missing)}")
    status = profile.get("status", "ACTIVE")
    if status != "ACTIVE":
        return Result("routing", FAIL, f"profile status {status}")
    regions = ", ".join(a.split(":")[3] for a in actual)
    return Result("routing", PASS, f"{len(actual)} model ARNs, all granted ({regions})")


def check_invoke(runtime: Any) -> Result:
    request: dict[str, Any] = {
        "modelId": config.BEDROCK_MODEL_ID,
        "messages": [{"role": "user", "content": [{"text": "Reply with OK."}]}],
        "inferenceConfig": {"maxTokens": 5, "temperature": 0},
    }
    if config.BEDROCK_GUARDRAIL_ID:
        request["guardrailConfig"] = {
            "guardrailIdentifier": config.BEDROCK_GUARDRAIL_ID,
            "guardrailVersion": config.BEDROCK_GUARDRAIL_VERSION or "DRAFT",
        }
    started = time.monotonic()
    try:
        response = runtime.converse(**request)
    except ClientError as e:
        error = e.response.get("Error", {})
        return Result("invoke", FAIL, f"{error.get('Code')}: {error.get('Message', '')}")
    except BotoCoreError as e:
        return Result("invoke", FAIL, str(e))
    ms = int((time.monotonic() - started) * 1000)
    usage = response.get("usage", {})
    server = response.get("metrics", {}).get("latencyMs")
    request_id = response.get("ResponseMetadata", {}).get("RequestId")
    guard = ", guardrail applied" if config.BEDROCK_GUARDRAIL_ID else ""
    return Result("invoke", PASS, f"{ms} ms client, {server} ms server, "
                  f"{usage.get('inputTokens')}/{usage.get('outputTokens')} tokens, "
                  f"request {request_id}{guard}")


def run(sts: Any, bedrock: Any, runtime: Any,
        resolve: Callable[..., list] = socket.getaddrinfo) -> list[Result]:
    results = [check_config()]
    if results[0].status == FAIL:
        return results
    identity = check_identity(sts)
    results.append(identity)
    results.append(check_network(config.BEDROCK_REGION, resolve))
    results.append(check_routing(bedrock))
    if identity.status == PASS:
        results.append(check_invoke(runtime))
    return results


def main() -> int:
    import boto3

    from oncall.diagnosis import bedrock as runtime_client

    region = config.BEDROCK_REGION
    results = run(
        boto3.client("sts", region_name=region),
        boto3.client("bedrock", region_name=region),
        runtime_client.default_client(),
    )
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.status}  {r.name:{width}}  {r.detail}")
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
