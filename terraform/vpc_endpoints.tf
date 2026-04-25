# VPC Endpoints — how private-subnet workloads reach AWS services without a
# NAT gateway or internet route.
#
# Two flavors to keep straight:
#
#   INTERFACE endpoint (PrivateLink)
#     Provisions one ENI per subnet with a private IP inside the VPC. When
#     private_dns_enabled = true, Route 53 Resolver intercepts the public
#     service hostname (e.g. ssm.us-east-1.amazonaws.com) inside the VPC
#     and returns the ENI IP instead. SDK code needs zero changes.
#     Billed ~$0.01/hr per endpoint per AZ + per-GB data processing. The
#     biggest ongoing cost in this stack — teardown target #1.
#
#   GATEWAY endpoint (S3 and DynamoDB only)
#     No ENI; the endpoint is a prefix-list route entry attached to a route
#     table. Traffic to the service's public IP range is redirected via the
#     AWS backbone. Free. Must attach to every route table whose subnets
#     need access — we have only one (private RT).
#
# Rule: one endpoint per AWS service we call. ECR needs two (ecr.api for
# control plane + ecr.dkr for layer data). ECR also stores image layers in
# S3, so we need the S3 gateway endpoint as well. SSM Session Manager needs
# three (ssm + ssmmessages + ec2messages). CloudWatch Logs is one. STS,
# Secrets Manager, etc. would each need their own; we have not added them
# because the app does not call them.
#
# Security model: endpoints_sg below gates who can talk to the endpoint
# ENIs. We allow the EC2 SG (by reference, not CIDR) on 443 only. Nothing
# else in the VPC reaches the endpoints.

resource "aws_security_group" "endpoints_sg" {
  name        = "${var.project_name}-endpoints-sg"
  description = "HTTPS from app EC2 to VPC endpoints"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name        = "${var.project_name}-endpoints-sg"
    Environment = var.environment
  }
}


resource "aws_vpc_security_group_ingress_rule" "endpoints_from_ec2" {
  security_group_id            = aws_security_group.endpoints_sg.id
  referenced_security_group_id = aws_security_group.ec2_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "HTTPS from ec2 to endpoints"
}

resource "aws_vpc_endpoint" "ssm" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ssm"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints_sg.id]
  private_dns_enabled = true

  tags = {
    Name        = "${var.project_name}-ssm-endpoint"
    Environment = var.environment
  }
}

resource "aws_vpc_endpoint" "ssmmessages" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ssmmessages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints_sg.id]
  private_dns_enabled = true

  tags = {
    Name        = "${var.project_name}-ssmmessages-endpoint"
    Environment = var.environment
  }
}

resource "aws_vpc_endpoint" "ec2messages" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ec2messages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints_sg.id]
  private_dns_enabled = true

  tags = {
    Name        = "${var.project_name}-ec2messages-endpoint"
    Environment = var.environment
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = {
    Name        = "${var.project_name}-s3-endpoint"
    Environment = var.environment
  }
}


resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints_sg.id]
  private_dns_enabled = true

  tags = {
    Name        = "${var.project_name}-ecr-api-endpoint"
    Environment = var.environment
  }
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints_sg.id]
  private_dns_enabled = true

  tags = {
    Name        = "${var.project_name}-ecr-dkr-endpoint"
    Environment = var.environment
  }
}


resource "aws_vpc_endpoint" "logs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints_sg.id]
  private_dns_enabled = true

  tags = {
    Name        = "${var.project_name}-logs-endpoint"
    Environment = var.environment
  }
}