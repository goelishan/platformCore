# IAM for the app tier.
#
# The instance profile is the EC2-specific wrapper that lets aws_instance
# attach an IAM role. The role itself holds three permission sets as of
# Phase 9:
#
#   1. AmazonSSMManagedInstanceCore (managed)
#        Minimum SSM Session Manager needs for agent registration and
#        shell access without SSH.
#   2. AmazonEC2ContainerRegistryReadOnly (managed)
#        ecr:GetAuthorizationToken + pull actions. Needed for user_data to
#        docker pull from our ECR repo.
#   3. platformcore-cw-logs-write (inline)
#        Scoped to /platformcore/* log groups only. Lets the awslogs Docker
#        driver create streams and PutLogEvents. Inline (not managed)
#        because it is conceptually part of this role's identity and never
#        wants to outlive it.
#
# Mental model: role = permissions; attachment = glue; instance profile =
# EC2-API passthrough. EC2 is the only AWS service that requires this extra
# wrapping; everywhere else (Lambda, ECS tasks, EKS pods via IRSA) attaches
# roles directly.
#
# Three-resource triangle for IAM-on-a-role:
#   aws_iam_role_policy_attachment  -> attaches an existing (managed) policy
#   aws_iam_role_policy             -> defines an inline policy on the role
#   aws_iam_policy + attachment     -> customer-managed reusable policy
# All three are used intentionally below; do not collapse them.

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2_ssm" {
  name               = "${var.project_name}-ec2-ssm-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = {
    Name        = "${var.project_name}-ec2-ssm-role"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2_ssm" {
  name = "${var.project_name}-ec2-ssm-profile"
  role = aws_iam_role.ec2_ssm.name

  tags = {
    Name        = "${var.project_name}-ec2-ssm-profile"
    Environment = var.environment
  }
}


resource "aws_iam_role_policy_attachment" "ecr_readonly" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy" "cw_logs_write" {
  name = "${var.project_name}-cw-logs-write"
  role = aws_iam_role.ec2_ssm.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
      ]
      Resource = "arn:aws:logs:*:*:log-group:/platformcore/*:*"
    }]
  })
}