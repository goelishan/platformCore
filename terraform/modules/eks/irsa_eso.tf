#--------------------------------------------------------------------------------------------------------
# IRSA - EXTERNAL SECRETS OPERATOR
#--------------------------------------------------------------------------------------------------------


data "aws_iam_policy_document" "eso_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.id]
    }

    condition {
      test = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values = ["system:serviceaccount:external-secrets:external-secrets"]
    }

    condition {
      test = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud"
      values = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "eso_secrets" {
  statement {
    actions = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
    ]

    resources = [
        "arn:aws:secretsmanager:${data.aws_region.fastapi.name}:${data.aws_caller_identity.fastapi.account_id}:secret:platformcore/*"
    ]
  }
}

resource "aws_iam_policy" "eso_secrets" {
  name        = "${var.project_name}-eso-secrets"
  description = "Allows ESO to read platformcore/* secrets from Secrets Manager"
  policy      = data.aws_iam_policy_document.eso_secrets.json
}

resource "aws_iam_role" "eso" {
  name               = "${var.project_name}-eso"
  assume_role_policy = data.aws_iam_policy_document.eso_trust.json
}

resource "aws_iam_role_policy_attachment" "eso_secrets" {
  role       = aws_iam_role.eso.name
  policy_arn = aws_iam_policy.eso_secrets.arn
}