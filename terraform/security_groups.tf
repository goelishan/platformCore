#--------------------------------------------------------------------------------------------------------
# CROSS-MODULE SECURITY GROUP RULES
#--------------------------------------------------------------------------------------------------------
#
# These rules connect SGs that live in different modules. They can't live
# inside any single module because each rule references two SGs from two
# different modules - putting the rule in either source or destination module
# would create a circular module-level dependency in Terraform's DAG.
#
# Same pattern as Day 8's standalone rule resources for circular SG deps,
# applied one level up at module scope. Separate the edges so the DAG can
# topologically sort them.
#
# Four rules in this file, one per cross-module edge:
#   alb_sg     -> ec2_sg          (egress, port 8000)   ALB forwards traffic
#   ec2_sg     <- alb_sg          (ingress, port 8000)  app accepts from ALB
#   rds_sg     <- ec2_sg          (ingress, port 5432)  Postgres from app
#   endpoints_sg <- ec2_sg        (ingress, port 443)   HTTPS to VPC endpoints



#--------------------------------------------------------------------------------------------------------
# ALB <-> EC2
#--------------------------------------------------------------------------------------------------------
#
# Identity-based authz (referenced_security_group_id, not CIDR). ALB
# autoscales/recreates; the SG membership identity does not. Same keystone
# pattern from Day 8.



resource "aws_vpc_security_group_egress_rule" "alb_to_ec2" {
  security_group_id            = module.edge.alb_sg_id
  referenced_security_group_id = module.compute.ec2_sg_id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
  description                  = "Forward to app tier on 8000"
}


resource "aws_vpc_security_group_ingress_rule" "ec2_from_alb" {
  security_group_id            = module.compute.ec2_sg_id
  referenced_security_group_id = module.edge.alb_sg_id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
  description                  = "Accept traffic from ALB on 8000"
}



#--------------------------------------------------------------------------------------------------------
# EC2 -> RDS
#--------------------------------------------------------------------------------------------------------
#
# Postgres ingress on rds_sg. Only sessions whose source SG is ec2_sg can
# reach 5432 - no public path, no CIDR allowlist, no exceptions.



resource "aws_vpc_security_group_ingress_rule" "rds_from_ec2" {
  security_group_id            = module.data.rds_sg_id
  referenced_security_group_id = module.compute.ec2_sg_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Postgres from app EC2 SG"
}



#--------------------------------------------------------------------------------------------------------
# EC2 -> VPC ENDPOINTS
#--------------------------------------------------------------------------------------------------------
#
# HTTPS ingress on endpoints_sg from ec2_sg. Required for the app EC2 to
# reach SSM, ECR, CloudWatch Logs via their interface endpoint ENIs.



resource "aws_vpc_security_group_ingress_rule" "endpoints_from_ec2" {
  security_group_id            = module.network.endpoints_sg_id
  referenced_security_group_id = module.compute.ec2_sg_id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  description                  = "HTTPS from app EC2 to VPC endpoints"
}

resource "aws_vpc_security_group_egress_rule" "ec2_to_endpoints" {
  security_group_id            = module.compute.ec2_sg_id
  referenced_security_group_id = module.network.endpoints_sg_id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  description                  = "EC2 to VPC endpoints on 443"
}

resource "aws_vpc_security_group_egress_rule" "ec2_to_rds" {
  security_group_id            = module.compute.ec2_sg_id
  referenced_security_group_id = module.data.rds_sg_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "EC2 to RDS on 5432"
}


data "aws_ec2_managed_prefix_list" "s3" {
  name = "com.amazonaws.${var.aws_region}.s3"
}

resource "aws_vpc_security_group_egress_rule" "ec2_to_s3" {
  security_group_id = module.compute.ec2_sg_id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.s3.id
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "s3 gateway endpoint"
}