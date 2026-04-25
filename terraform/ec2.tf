# Application compute - first EC2 instance in PlatformCore.
#
# - ECS-optimized AL2023 AMI, not base AL2023. Base image needs dnf install
#   at first boot to get Docker, but private subnets with only VPC endpoints
#   (no NAT) can't reach AL2023's CloudFront-hosted package mirrors. ECS-
#   optimized ships Docker + amazon-ecr-credential-helper pre-baked, so the
#   app can run without internet bootstrap. In production, Packer-built
#   custom AMIs are the preferred pattern.

# - Private subnet placement. The ALB (public subnets) is the only thing that
#   reaches this EC2 on 8000; no path from the internet.

# - No SSH: no key_name, no port 22 in any SG. Shell access via SSM Session
#   Manager using the instance profile from iam.tf.

# - IMDSv2 enforced (http_tokens=required). IMDSv1 is vulnerable to SSRF-based
#   attacks that steal the instance's IAM credentials. Mandatory hardening.

# - Root volume encrypted at rest via the AWS-managed EBS KMS key. Swap to a
#   customer-managed key when compliance (SOC2/HIPAA/PCI) demands it.



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

resource "aws_instance" "app" {
  ami                         = data.aws_ami.al2023_ecs.id
  instance_type               = "t3.micro"
  subnet_id                   = aws_subnet.private[0].id
  vpc_security_group_ids      = [aws_security_group.ec2_sg.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2_ssm.name
  associate_public_ip_address = false
  user_data                   = local.user_data
  user_data_replace_on_change = true

  # Force IMDSv2 (token-based). Prevents SSRF exploits from stealing the
  # attached IAM role's temporary credentials via the 169.254.169.254 endpoint.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Encrypt root volume at rest. 30 GB is the AL2023 AMI's minimum snapshot
  # size; anything smaller is rejected by RunInstances. gp3 is the cost /
  # performance sweet spot for general-purpose workloads.
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



#--------------------------------------------------------------------------------------------------------
# USER DATA
#--------------------------------------------------------------------------------------------------------
#
# Runs once on first boot as root via cloud-init. Anything that fails here
# fails silently from Terraform's perspective — apply succeeds on AWS API
# accept, not on script success. Read /var/log/user-data.log via SSM or
# `aws ec2 get-console-output` to debug.
#
# The image tag is pinned to :v2 here (lazy-DB build). Bumping the tag in
# this file + `terraform apply` triggers instance replacement because we
# set user_data_replace_on_change = true on the aws_instance resource. In
# production this pattern gets replaced by launch templates + ASG rolling
# deployments; for a single learning instance it is the pragmatic choice.
#
# APP_VERSION is injected as an env var so the running container can report
# its tag via the /version endpoint. Wire it at deploy time so the app does
# not need to be rebuilt just to bump the reported version.

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
    # In production this lives in a Packer-built custom AMI; here it lives
    # in user_data because we are using AWS's stock AMI directly.
    systemctl stop ecs 2>/dev/null || true
    systemctl disable ecs 2>/dev/null || true
    docker rm -f ecs-agent 2>/dev/null || true

    # Create the CloudWatch log group explicitly so we can set retention.
    # The awslogs Docker driver creates log *streams* on demand but NOT the
    # group — letting it auto-create would leave us with infinite retention
    # (infinite cost) and no way to attach a policy before the first write.
    aws logs create-log-group --log-group-name /platformcore/app \
      --region ${var.aws_region} 2>/dev/null || true
    aws logs put-retention-policy --log-group-name /platformcore/app \
      --retention-in-days 7 --region ${var.aws_region} || true

    # Authenticate Docker with ECR using the instance-profile credentials.
    # The token is valid for 12 hours; we only need it long enough for the
    # pull below.
    aws ecr get-login-password --region ${var.aws_region} | \
      docker login --username AWS --password-stdin \
      ${aws_ecr_repository.app.repository_url}

    # Pull flows: DNS -> ecr.dkr endpoint (manifest) -> s3 gateway (layers).
    docker pull ${aws_ecr_repository.app.repository_url}:${local.app_image_tag}

    # Run the container.
    #   --restart unless-stopped: survives reboots and daemon restarts.
    #   --log-driver=awslogs: stdout/stderr stream to CloudWatch Logs.
    #   -e APP_VERSION: surfaced by the app's /version endpoint.
    docker run -d \
      --name platformcore-app \
      --restart unless-stopped \
      -p 8000:8000 \
      -e APP_VERSION=${local.app_image_tag} \
      --log-driver=awslogs \
      --log-opt awslogs-region=${var.aws_region} \
      --log-opt awslogs-group=/platformcore/app \
      --log-opt awslogs-create-group=false \
      --log-opt awslogs-stream=$(hostname) \
      ${aws_ecr_repository.app.repository_url}:${local.app_image_tag}

    echo "=== user-data done: $(date -u) ==="
  EOF
}
