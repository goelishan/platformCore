# Network topology - VPC, subnets, IGW, route tables.
#
# Layout: one /16 VPC with two public /24 subnets and two private /24 subnets
# spread across two AZs. Public subnets carry 0.0.0.0/0 -> IGW; private
# subnets deliberately have NO default route to the internet (no NAT gateway).
# Outbound from the private subnet is strictly via VPC Endpoints (see
# vpc_endpoints.tf) — an Option-A design: expensive NAT hourly charge
# replaced with a set of per-service interface/gateway endpoints. Safer
# (AWS-backbone-only traffic), cheaper at our scale, and slightly more work
# to maintain (one endpoint per AWS service we call).
#
# The private route table below exists explicitly so we have somewhere to
# attach the S3 gateway endpoint's route entry. Without an explicit private
# RT, private subnets default-associate to the VPC's main RT, which we do
# not own cleanly. Making it explicit avoids that implicit coupling.
#
# The key mechanism to remember: public vs private is defined BY THE ROUTE
# TABLE, not by the subnet's name or the map_public_ip_on_launch flag.
# The route entry is what makes a subnet public.

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


# Private route table is intentionally empty of explicit routes. The VPC's
# implicit local route (10.0.0.0/16 -> local) handles in-VPC traffic. The
# S3 gateway endpoint (vpc_endpoints.tf) attaches its prefix-list route here
# when applied. No 0.0.0.0/0 entry -> no internet access, by design.
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