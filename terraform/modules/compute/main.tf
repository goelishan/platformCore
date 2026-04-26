#--------------------------------------------------------------------------------------------------------
# COMPUTE MODULE
#--------------------------------------------------------------------------------------------------------
#
# Workload tier: EC2 instance, ECR registry, IAM role + instance profile,
# CloudWatch log group, and ec2_sg. Everything the running container needs,
# in one module.
#
# Key design choices:
#   ECS-optimized AL2023 AMI    - Docker pre-baked; no first-boot package
#                                  install, which is mandatory in a private
#                                  subnet with no NAT.
#   Private subnet placement    - no public IP; ALB is the only ingress path.
#   No SSH                      - SSM Session Manager replaces it. No
#                                  key_name, no port 22.
#   IMDSv2 enforced             - blocks SSRF -> credential theft (Capital
#                                  One 2019 was an IMDSv1 exploit).
#   Root volume encrypted       - AWS-managed EBS KMS key. CMK for SOC2/PCI.
#   Day 12 tech debt            - DATABASE_URL is templated into user_data
#                                  with the password in cleartext. Will move
#                                  to AWS Secrets Manager + secretsmanager
#                                  endpoint.



#--------------------------------------------------------------------------------------------------------
# DATA SOURCES
#--------------------------------------------------------------------------------------------------------
#
# AMI lookup pinned to the ECS-optimized AL2023 family. The ecs-hvm token in
# the name filter prefix locks the variant - a loose filter (al2023-ami-*)
# combined with most_recent is a hidden drift vector because Amazon publishing
# a different variant under the same prefix would silently flip the AMI on
# the next apply.



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


data "aws_ami" "al2023_ecs" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-ecs-hvm-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}



#--------------------------------------------------------------------------------------------------------
# IAM - ROLE, INSTANCE PROFILE, POLICY ATTACHMENTS
#--------------------------------------------------------------------------------------------------------
#
# Three permission sets via three resource shapes (the IAM triangle):
#   AmazonSSMManagedInstanceCore          managed, attached
#   AmazonEC2ContainerRegistryReadOnly    managed, attached
#   platformcore-cw-logs-write            inline, scoped to /platformcore/*
#
# The instance profile is the EC2-API passthrough required by RunInstances -
# EC2 is the odd service that can't attach a role directly. Lambda, ECS
# tasks, EKS pods (via IRSA) all attach roles directly; instance profiles
# are legacy plumbing, not a richer abstraction.



resource "aws_iam_role" "ec2_ssm" {
  name               = "${var.project_name}-ec2-ssm-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = {
    Name        = "${var.project_name}-ec2-ssm-role"
    Environment = var.environment
  }
}


resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}


resource "aws_iam_role_policy_attachment" "ecr_readonly" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}


resource "aws_iam_role_policy" "cw_logs_write" {
  name = "${var.project_name}-cw-logs-write"
  role = aws_iam_role.ec2_ssm.id

  # Two actions only. CreateLogGroup + PutRetentionPolicy were removed when
  # the log group became Terraform-managed on Day 10. Trailing :* on the
  # ARN matches log streams within the group; without it PutLogEvents fails.
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


resource "aws_iam_instance_profile" "ec2_ssm" {
  name = "${var.project_name}-ec2-ssm-profile"
  role = aws_iam_role.ec2_ssm.name

  tags = {
    Name        = "${var.project_name}-ec2-ssm-profile"
    Environment = var.environment
  }
}



#--------------------------------------------------------------------------------------------------------
# SECURITY GROUP
#--------------------------------------------------------------------------------------------------------
#
# ec2_sg owns its identity + its outbound rule. Inbound (from ALB), outbound
# to other modules' SGs (endpoints, RDS), and the ALB-side cross-module
# rules all live at root.
#
# The wide-open egress (-1, 0.0.0.0/0) is bounded by the route table, not
# the SG: the private subnet has no 0.0.0.0/0 route, so traffic to the
# internet has nowhere to go. The effective egress surface is "AWS services
# we have endpoints for + anything in the VPC." Route table is doing the
# containment, not the SG.



resource "aws_security_group" "ec2_sg" {
  name        = "${var.project_name}-ec2-sg"
  description = "App tier - accepts traffic only from the ALB"
  vpc_id      = var.vpc_id

  tags = {
    Name        = "${var.project_name}-ec2-sg"
    Environment = var.environment
  }
}


resource "aws_vpc_security_group_egress_rule" "ec2_all_outbound" {
  security_group_id = aws_security_group.ec2_sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "All outbound - constrained by private subnet route table"
}



#--------------------------------------------------------------------------------------------------------
# CLOUDWATCH LOG GROUP
#--------------------------------------------------------------------------------------------------------
#
# Terraform-managed (adopted via terraform import on Day 10). Retention
# lives on the resource as declarative config, not in user_data - the whole
# class of "silent retention bug" is structurally impossible because there's
# no runtime API call to fail. The awslogs Docker driver writes streams
# under this group at runtime; the group itself is apply-time.



resource "aws_cloudwatch_log_group" "app" {
  name              = "/platformcore/app"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-app-logs"
    Environment = var.environment
  }
}



#--------------------------------------------------------------------------------------------------------
# ECR REPOSITORY
#--------------------------------------------------------------------------------------------------------
#
# Private container registry for the app image. ECR layer blobs live in S3
# behind the scenes - that's why a private-subnet pull needs three endpoints
# (ecr.api control + ecr.dkr data + s3 gateway for blobs).
#
# image_tag_mutability = MUTABLE for dev iteration; production flips to
# IMMUTABLE so :latest and :v2 cannot be silently overwritten (supply-chain
# hygiene). scan_on_push runs the AWS-curated CVE scan against every push.
# Lifecycle policy uses camelCase keys (ECR native schema, against
# Terraform's snake_case convention - easy gotcha).



resource "aws_ecr_repository" "app" {
  name                 = "${var.project_name}-app"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name        = "${var.project_name}-app"
    Environment = var.environment
  }
}


resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 1 day"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 1
      }
      action = { type = "expire" }
    }]
  })
}



#--------------------------------------------------------------------------------------------------------
# EC2 - USER DATA
#--------------------------------------------------------------------------------------------------------
#
# Runs once on first boot via cloud-init. Failures here fail silently from
# Terraform's perspective - apply succeeds on AWS API accept, not on script
# success. Read /var/log/user-data.log via SSM or `aws ec2 get-console-output`
# to debug.
#
# user_data_replace_on_change = true on the instance below means any edit
# here forces instance replacement. APP_VERSION is injected as an env var
# so the running container reports its tag via /version without needing a
# rebuild for version-string changes. DATABASE_URL is templated from
# var.db_* inputs - the password is sensitive and propagates the redaction
# flag through Terraform's plan output.



locals {
  app_image_tag = "v2"

  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail
    exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

    echo "=== user-data start: $(date -u) ==="

    # ECS-optimized AMI ships Docker pre-installed; this is idempotent.
    systemctl enable --now docker

    # Disable the ECS agent that ECS-optimized AL2023 launches by default.
    # We use raw Docker, not ECS, so the agent's failed registration loop
    # (every ~3min trying to join a non-existent cluster) is pure noise.
    systemctl stop ecs 2>/dev/null || true
    systemctl disable ecs 2>/dev/null || true
    docker rm -f ecs-agent 2>/dev/null || true

    # Authenticate Docker with ECR using the instance-profile credentials.
    # The token is valid for 12 hours; we only need it long enough for the
    # pull below.
    aws ecr get-login-password --region ${var.aws_region} | \
      docker login --username AWS --password-stdin \
      ${aws_ecr_repository.app.repository_url}

    # Pull flow: DNS -> ecr.dkr endpoint (manifest) -> s3 gateway (layers).
    docker pull ${aws_ecr_repository.app.repository_url}:${local.app_image_tag}

    # Run the container.
    #   --restart unless-stopped: survives reboots and daemon restarts.
    #   --log-driver=awslogs: stdout/stderr stream to CloudWatch Logs.
    #   -e APP_VERSION: surfaced by the app's /version endpoint.
    #   -e DATABASE_URL: read by main.py on first /ready hit (lazy-DB).
    docker run -d \
      --name platformcore-app \
      --restart unless-stopped \
      -p 8000:8000 \
      -e APP_VERSION=${local.app_image_tag} \
      -e DATABASE_URL=postgresql://${var.db_username}:${var.db_password}@${var.db_endpoint}/${var.db_name} \
      --log-driver=awslogs \
      --log-opt awslogs-region=${var.aws_region} \
      --log-opt awslogs-group=/platformcore/app \
      --log-opt awslogs-create-group=false \
      --log-opt awslogs-stream=$(hostname) \
      ${aws_ecr_repository.app.repository_url}:${local.app_image_tag}

    echo "=== user-data done: $(date -u) ==="
  EOF
}



#--------------------------------------------------------------------------------------------------------
# EC2 INSTANCE
#--------------------------------------------------------------------------------------------------------
#
# Single t3.micro in private subnet [0]. Shell access via SSM Session Manager
# (no SSH key, no port 22). IMDSv2 required + hop_limit = 1 for SSRF defense
# in depth. Root volume gp3 30 GB encrypted (AL2023 minimum is 30 GB; 20 was
# rejected by RunInstances on first apply).



resource "aws_instance" "app" {
  ami                         = data.aws_ami.al2023_ecs.id
  instance_type               = "t3.micro"
  subnet_id                   = var.private_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.ec2_sg.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2_ssm.name
  associate_public_ip_address = false
  user_data                   = local.user_data
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name        = "${var.project_name}-app-ec2-root"
      Environment = var.environment
    }
  }

  tags = {
    Name        = "${var.project_name}-app-ec2"
    Environment = var.environment
  }
}
