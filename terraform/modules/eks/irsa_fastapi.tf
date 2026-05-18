#--------------------------------------------------------------------------------------------------------
# IRSA - FASTAPI
#--------------------------------------------------------------------------------------------------------

data "aws_caller_identity" "fastapi" {}
data "aws_region" "fastapi" {}

data "aws_iam_policy_document" "fastapi_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
        type="Federated"
        identifiers = [aws_iam_openid_connect_provider.eks.id]
    }
    condition {
      test = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values   = ["system:serviceaccount:platformcore:fastapi"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
}
}

data "aws_iam_policy_document" "fastapi_rds" {
  statement {
    actions   = ["rds-db:connect"]
    resources = [
      "arn:aws:rds-db:${data.aws_region.fastapi.name}:${data.aws_caller_identity.fastapi.account_id}:dbuser:${var.db_resource_id}/fastapi"
    ]
  }
}

resource "aws_iam_policy" "fastapi_rds" {
  name        = "${var.project_name}-fastapi-rds-connect"
  description = "Allows fastapi Pod to connect to RDS as the fastapi IAM-auth DB user"
  policy      = data.aws_iam_policy_document.fastapi_rds.json
}

resource "aws_iam_role" "fastapi" {
  name               = "${var.project_name}-fastapi"
  assume_role_policy = data.aws_iam_policy_document.fastapi_trust.json
}

resource "aws_iam_role_policy_attachment" "fastapi_rds" {
  role       = aws_iam_role.fastapi.name
  policy_arn = aws_iam_policy.fastapi_rds.arn
}
    
