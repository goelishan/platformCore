#--------------------------------------------------------------------------------------------------------
# IRSA - ONCALL AGENT
#--------------------------------------------------------------------------------------------------------
#
#   - trusted only by the agent's ServiceAccount, through the cluster OIDC provider
#
#   - bedrock:InvokeModel on one inference profile and the foundation model in each region
#     that profile routes to, nothing wider
#
#   - rds-db:connect as the oncall IAM-auth database user only
#
# Two policies on one role, one per dependency. With a single policy, detaching it to cut
# Bedrock off also revoked the store login (drill D3, 2026-10-13): new connections failed
# with "PAM authentication failed" while sessions opened earlier kept working, so nothing
# noticed. Separate policies keep one dependency's incident from silently becoming two,
# and let each be revoked, audited and drilled on its own.
#
# Converse is authorised as bedrock:InvokeModel. A cross-region profile needs both ARNs:
# the profile is what the agent calls, and each request is executed against the
# foundation model in whichever of the profile's regions Bedrock picks, so a policy
# naming only the profile is denied on the model, and only the model is denied on the
# profile.
#
#   - bedrock:GetInferenceProfile on the same profile, read-only, so the preflight
#     (make bedrock-check) can compare where the profile routes with what this policy
#     grants, from inside the cluster where a mismatch would actually bite
#
#   - bedrock:ApplyGuardrail only when a guardrail ARN is set; a Converse request that
#     names a guardrail is denied without it, and an unused grant is one more to audit



data "aws_caller_identity" "oncall" {}
data "aws_region" "oncall" {}

locals {
  oncall_oidc_host = replace(aws_iam_openid_connect_provider.eks.url, "https://", "")

  oncall_profile_arn = "arn:aws:bedrock:${data.aws_region.oncall.name}:${data.aws_caller_identity.oncall.account_id}:inference-profile/${var.oncall_bedrock_inference_profile}"

  oncall_bedrock_resources = concat(
    [local.oncall_profile_arn],
    [
      for region in var.oncall_bedrock_model_regions :
      "arn:aws:bedrock:${region}::foundation-model/${var.oncall_bedrock_foundation_model}"
    ],
  )
}

data "aws_iam_policy_document" "oncall_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oncall_oidc_host}:sub"
      values   = ["system:serviceaccount:${var.oncall_namespace}:${var.oncall_service_account}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oncall_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "oncall_bedrock" {
  statement {
    sid       = "InvokeOneModelThroughOneProfile"
    actions   = ["bedrock:InvokeModel"]
    resources = local.oncall_bedrock_resources
  }

  statement {
    sid       = "ReadTheProfileItCalls"
    actions   = ["bedrock:GetInferenceProfile"]
    resources = [local.oncall_profile_arn]
  }

  dynamic "statement" {
    for_each = var.oncall_bedrock_guardrail_arn == "" ? [] : [var.oncall_bedrock_guardrail_arn]
    content {
      sid       = "ApplyItsGuardrail"
      actions   = ["bedrock:ApplyGuardrail"]
      resources = [statement.value]
    }
  }
}

data "aws_iam_policy_document" "oncall_store" {
  statement {
    sid     = "ConnectAsOncallDatabaseUser"
    actions = ["rds-db:connect"]
    resources = [
      "arn:aws:rds-db:${data.aws_region.oncall.name}:${data.aws_caller_identity.oncall.account_id}:dbuser:${var.db_resource_id}/oncall"
    ]
  }
}

resource "aws_iam_policy" "oncall_bedrock" {
  name        = "${var.project_name}-oncall-bedrock"
  description = "On-call agent: one Bedrock inference profile and the models it routes to"
  policy      = data.aws_iam_policy_document.oncall_bedrock.json
}

resource "aws_iam_policy" "oncall_store" {
  name        = "${var.project_name}-oncall-store"
  description = "On-call agent: RDS IAM authentication as the oncall database user"
  policy      = data.aws_iam_policy_document.oncall_store.json
}

resource "aws_iam_role" "oncall" {
  name               = "${var.project_name}-oncall-agent"
  assume_role_policy = data.aws_iam_policy_document.oncall_trust.json
}

resource "aws_iam_role_policy_attachment" "oncall_bedrock" {
  role       = aws_iam_role.oncall.name
  policy_arn = aws_iam_policy.oncall_bedrock.arn
}

resource "aws_iam_role_policy_attachment" "oncall_store" {
  role       = aws_iam_role.oncall.name
  policy_arn = aws_iam_policy.oncall_store.arn
}
