#--------------------------------------------------------------------------------------------------------
# IRSA - AWS LOAD BALANCER CONTROLLER
#--------------------------------------------------------------------------------------------------------

data "aws_iam_policy_document" "alb_controller_trust" {
    statement {
      actions = ["sts:AssumeRoleWithWebIdentity"]

      principals {
        type = "Federated"
        identifiers = [aws_iam_openid_connect_provider.eks.id]
      }

      condition {
        test = "StringEquals"
        variable = "${replace(aws_iam_openid_connect_provider.eks.url,"https://", "")}:sub"
        values = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
      }

      condition {
        test = "StringEquals"
        variable = "${replace(aws_iam_openid_connect_provider.eks.url,"https://", "")}:aud"
        values = ["sts.amazonaws.com"]
      }
    }
}

#--------------------------------------------------------------------------------------------------------
# PERMISSIONS POLICY
#--------------------------------------------------------------------------------------------------------

data "http" "alb_controller_policy" {
    url = "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.8.2/docs/install/iam_policy.json"

    request_headers = {
        Accept = "application/json"
    }
}

resource "aws_iam_policy" "alb_controller" {
  name = "${var.project_name}-alb-controller"
  description = "Permissions for ALB controller"
  policy = data.http.alb_controller_policy.response_body
}

#--------------------------------------------------------------------------------------------------------
# ROLE + ATTACHMENT
#--------------------------------------------------------------------------------------------------------

resource "aws_iam_role" "alb_controller" {
    name="${var.project_name}-alb-controller"
    assume_role_policy = data.aws_iam_policy_document.alb_controller_trust.json
}

resource "aws_iam_role_policy_attachment" "alb_controller" {
  role       = aws_iam_role.alb_controller.name
  policy_arn = aws_iam_policy.alb_controller.arn
}