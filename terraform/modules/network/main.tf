#--------------------------------------------------------------------------------------------------------
# NETWORK MODULE
#--------------------------------------------------------------------------------------------------------
#
# VPC, subnets across two AZs, IGW, route tables, and the full VPC endpoint
# stack (Option A — no NAT). Every other module sits on top of this.
#
# Public vs private is defined by the route table, not by the subnet's name
# or the map_public_ip_on_launch flag. The public RT carries 0.0.0.0/0 -> IGW;
# the private RT has only the implicit local route plus the S3 gateway
# endpoint's prefix-list entry. Remove the IGW route and the public subnet
# is effectively private.



#--------------------------------------------------------------------------------------------------------
# VPC + AZ DATA
#--------------------------------------------------------------------------------------------------------
#
# Dynamic AZ lookup over hardcoded names — if an AZ goes into maintenance,
# Terraform picks a healthy one. Hardcoding creates silent single-AZ risk.



resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name        = "${var.project_name}-vpc"
    Environment = var.environment
  }
}


data "aws_availability_zones" "available" {
  state = "available"
}



#--------------------------------------------------------------------------------------------------------
# SUBNETS
#--------------------------------------------------------------------------------------------------------
#
# Two /24 public + two /24 private across two AZs. cidrsubnet(/16, 8, N) carves
# /24 blocks from the VPC CIDR. The +10 offset on private indices is a
# readability convention (10.0.10.x, 10.0.11.x), not an AWS requirement.



resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  map_public_ip_on_launch = true

  tags = {
    Name        = "${var.project_name}-public-${count.index + 1}"
    Environment = var.environment
  }
}


resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name        = "${var.project_name}-private-${count.index + 1}"
    Environment = var.environment
  }
}



#--------------------------------------------------------------------------------------------------------
# INTERNET GATEWAY + ROUTE TABLES
#--------------------------------------------------------------------------------------------------------
#
# Public RT routes 0.0.0.0/0 to the IGW. Private RT exists explicitly so we
# have somewhere to attach the S3 gateway endpoint route entry — without an
# explicit private RT, private subnets would default-associate to the VPC's
# main RT, an implicit coupling we don't want to own.



resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name        = "${var.project_name}-igw"
    Environment = var.environment
  }
}


resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name        = "${var.project_name}-public-rt"
    Environment = var.environment
  }
}


resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}


resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name        = "${var.project_name}-private-rt"
    Environment = var.environment
  }
}


resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}



#--------------------------------------------------------------------------------------------------------
# VPC ENDPOINTS - SECURITY GROUP
#--------------------------------------------------------------------------------------------------------
#
# endpoints_sg gates traffic to all interface endpoint ENIs. The ingress rule
# (HTTPS from ec2_sg) lives cross-module at root because ec2_sg is in the
# compute module and Terraform's DAG can't sort circular module references.



resource "aws_security_group" "endpoints_sg" {
  name        = "${var.project_name}-endpoints-sg"
  description = "HTTPS from app EC2 to VPC endpoints"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name        = "${var.project_name}-endpoints-sg"
    Environment = var.environment
  }
}



#--------------------------------------------------------------------------------------------------------
# VPC ENDPOINTS - INTERFACE
#--------------------------------------------------------------------------------------------------------
#
# One ENI per private subnet per service. private_dns_enabled = true makes
# Route 53 Resolver intercept the public service hostname and return the ENI
# IP — SDK code needs zero changes. Billed per-hour per-AZ; the biggest
# ongoing cost in this stack and teardown target #1.
#
# SSM Session Manager needs three (ssm + ssmmessages + ec2messages); ECR
# needs two (ecr.api control plane + ecr.dkr data plane); CloudWatch Logs
# is one. STS deliberately omitted — get-login-password uses instance-profile
# credentials directly via SigV4 without identity lookups.



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



#--------------------------------------------------------------------------------------------------------
# VPC ENDPOINT - GATEWAY (S3)
#--------------------------------------------------------------------------------------------------------
#
# Free; route-table-attached prefix list, no ENI. Required for ECR layer
# blob fetches — ECR stores image layers in S3 buckets behind the scenes.
# Drop this and docker pull hangs at the first layer.



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
