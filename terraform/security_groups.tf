# Security groups for PlatformCore's first compute tier.
#
# Two SGs: ALB (internet-facing front door) and EC2 (app tier).
# All rules are defined as standalone resources (aws_vpc_security_group_*_rule)
# rather than inline ingress/egress blocks — this is the AWS-provider-v5+ style
# and is MANDATORY for any rule that needs to reference another SG, because
# inline cross-references create a circular dependency in Terraform's graph.
#
# Rule of thumb: for a given SG, pick *either* all-inline *or* all-standalone.
# Mixing the two makes Terraform fight its own state (it removes rules it
# doesn't know about on each apply). We're going all-standalone here.

resource "aws_security_group" "alb_sg" {
  name        = "${var.project_name}-alb-sg"
  description = "ALB front door - accepts HTTP from the internet"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name        = "${var.project_name}-alb-sg"
    Environment = var.environment
  }
}

resource "aws_security_group" "ec2_sg" {
  name        = "${var.project_name}-ec2-sg"
  description = "App tier - accepts traffic only from the ALB"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name        = "${var.project_name}-ec2-sg"
    Environment = var.environment
  }
}

# -----------------------------------------------------------------------------
# ALB SG rules
# -----------------------------------------------------------------------------

# Ingress: internet → ALB on 80 (HTTP). Shortcut for today; production should
# terminate on 443 only and redirect 80 → 443. Tracked as a Phase-2-late fix.
resource "aws_vpc_security_group_ingress_rule" "alb_http_from_internet" {
  security_group_id = aws_security_group.alb_sg.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "HTTP from internet"
}

# Egress: ALB → EC2 SG on 8000. Tightened to the EC2 SG only; we deliberately
# don't fall back to AWS's default-allow-all egress.
resource "aws_vpc_security_group_egress_rule" "alb_to_ec2" {
  security_group_id            = aws_security_group.alb_sg.id
  referenced_security_group_id = aws_security_group.ec2_sg.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
  description                  = "Forward to app tier on 8000"
}

# -----------------------------------------------------------------------------
# EC2 SG rules
# -----------------------------------------------------------------------------

# Ingress: ALB SG → EC2 on 8000. Source is the ALB's SG identity, not a CIDR —
# this is the keystone of the design. ALB autoscales/relocates; the SG membership
# does not.
resource "aws_vpc_security_group_ingress_rule" "ec2_from_alb" {
  security_group_id            = aws_security_group.ec2_sg.id
  referenced_security_group_id = aws_security_group.alb_sg.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
  description                  = "Accept traffic from ALB on 8000"
}

# Egress: EC2 → 0.0.0.0/0 on all protocols.
#
# Looks scary at a glance but is bounded by the route table, not the SG:
# the private subnet has NO 0.0.0.0/0 route (see vpc.tf), so traffic
# destined to the public internet has nowhere to go. Packets to AWS
# services get redirected by the VPC endpoint private-DNS overrides to
# endpoint ENIs (also in 10.0.0.0/16), which the implicit local route
# handles.
#
# Result: the effective egress surface is "AWS services we have endpoints
# for" + "anything else in the VPC". Tightening the SG to the VPC CIDR
# would be cosmetic today and would break the moment we add any endpoint
# whose private-DNS IP is outside the VPC (rare but possible with
# PrivateLink). Leaving it -1 is the defensible choice; the route table
# is doing the actual containment work.
resource "aws_vpc_security_group_egress_rule" "ec2_all_outbound" {
  security_group_id = aws_security_group.ec2_sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "All outbound - constrained by private subnet route table"
}
