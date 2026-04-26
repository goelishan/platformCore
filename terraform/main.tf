#--------------------------------------------------------------------------------------------------------
# ROOT MODULE - PLATFORMCORE
#--------------------------------------------------------------------------------------------------------
#
# Instantiates four child modules in dependency order: network -> data ->
# edge -> compute. Cross-module security group rules live in security_groups.tf
# at this level (they reference SG IDs from multiple modules - circular at
# module scope, broken by separating the rule resources).
#
# moved blocks below relocated existing flat-file resources into their new
# module addresses on Day 11. They are idempotent once applied; safe to
# delete in a future cleanup commit.



data "aws_caller_identity" "current" {}



#--------------------------------------------------------------------------------------------------------
# NETWORK MODULE
#--------------------------------------------------------------------------------------------------------



module "network" {
  source = "./modules/network"

  project_name = var.project_name
  environment  = var.environment
  aws_region   = var.aws_region
  vpc_cidr     = var.vpc_cidr
}


moved {
  from = aws_vpc.main
  to   = module.network.aws_vpc.main
}

moved {
  from = aws_subnet.public
  to   = module.network.aws_subnet.public
}

moved {
  from = aws_subnet.private
  to   = module.network.aws_subnet.private
}

moved {
  from = aws_internet_gateway.main
  to   = module.network.aws_internet_gateway.main
}

moved {
  from = aws_route_table.public
  to   = module.network.aws_route_table.public
}

moved {
  from = aws_route_table.private
  to   = module.network.aws_route_table.private
}

moved {
  from = aws_route_table_association.public
  to   = module.network.aws_route_table_association.public
}

moved {
  from = aws_route_table_association.private
  to   = module.network.aws_route_table_association.private
}

moved {
  from = aws_vpc_endpoint.ssm
  to   = module.network.aws_vpc_endpoint.ssm
}

moved {
  from = aws_vpc_endpoint.ssmmessages
  to   = module.network.aws_vpc_endpoint.ssmmessages
}

moved {
  from = aws_vpc_endpoint.ec2messages
  to   = module.network.aws_vpc_endpoint.ec2messages
}

moved {
  from = aws_vpc_endpoint.ecr_api
  to   = module.network.aws_vpc_endpoint.ecr_api
}

moved {
  from = aws_vpc_endpoint.ecr_dkr
  to   = module.network.aws_vpc_endpoint.ecr_dkr
}

moved {
  from = aws_vpc_endpoint.logs
  to   = module.network.aws_vpc_endpoint.logs
}

moved {
  from = aws_vpc_endpoint.s3
  to   = module.network.aws_vpc_endpoint.s3
}

moved {
  from = aws_security_group.endpoints_sg
  to   = module.network.aws_security_group.endpoints_sg
}



#--------------------------------------------------------------------------------------------------------
# DATA MODULE
#--------------------------------------------------------------------------------------------------------



module "data" {
  source = "./modules/data"

  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  project_name       = var.project_name
  environment        = var.environment
}


moved {
  from = aws_db_instance.main
  to   = module.data.aws_db_instance.main
}

moved {
  from = aws_db_subnet_group.main
  to   = module.data.aws_db_subnet_group.main
}

moved {
  from = aws_db_parameter_group.main
  to   = module.data.aws_db_parameter_group.main
}

moved {
  from = aws_security_group.rds_sg
  to   = module.data.aws_security_group.rds_sg
}

moved {
  from = random_password.rds_master
  to   = module.data.random_password.rds_master
}



#--------------------------------------------------------------------------------------------------------
# COMPUTE MODULE
#--------------------------------------------------------------------------------------------------------
#
# Depends on data module for DB connection inputs (endpoint, username,
# password, db_name). The password input is sensitive and propagates the
# redaction flag into compute's user_data.



module "compute" {
  source = "./modules/compute"

  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  aws_region         = var.aws_region
  project_name       = var.project_name
  environment        = var.environment

  db_endpoint = module.data.endpoint
  db_username = module.data.username
  db_password = module.data.password
  db_name     = module.data.db_name
}


moved {
  from = aws_instance.app
  to   = module.compute.aws_instance.app
}

moved {
  from = aws_iam_role.ec2_ssm
  to   = module.compute.aws_iam_role.ec2_ssm
}

moved {
  from = aws_iam_instance_profile.ec2_ssm
  to   = module.compute.aws_iam_instance_profile.ec2_ssm
}

moved {
  from = aws_iam_role_policy_attachment.ec2_ssm
  to   = module.compute.aws_iam_role_policy_attachment.ssm_core
}

moved {
  from = aws_iam_role_policy_attachment.ecr_readonly
  to   = module.compute.aws_iam_role_policy_attachment.ecr_readonly
}

moved {
  from = aws_iam_role_policy.cw_logs_write
  to   = module.compute.aws_iam_role_policy.cw_logs_write
}

moved {
  from = aws_cloudwatch_log_group.app
  to   = module.compute.aws_cloudwatch_log_group.app
}

moved {
  from = aws_ecr_repository.app
  to   = module.compute.aws_ecr_repository.app
}

moved {
  from = aws_ecr_lifecycle_policy.app
  to   = module.compute.aws_ecr_lifecycle_policy.app
}

moved {
  from = aws_security_group.ec2_sg
  to   = module.compute.aws_security_group.ec2_sg
}

moved {
  from = aws_vpc_security_group_egress_rule.ec2_all_outbound
  to   = module.compute.aws_vpc_security_group_egress_rule.ec2_all_outbound
}



#--------------------------------------------------------------------------------------------------------
# EDGE MODULE
#--------------------------------------------------------------------------------------------------------
#
# Depends on compute for instance_id (target group attachment binds the EC2).



module "edge" {
  source = "./modules/edge"

  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  instance_id       = module.compute.instance_id
  project_name      = var.project_name
  environment       = var.environment
}


moved {
  from = aws_lb.app
  to   = module.edge.aws_lb.app
}

moved {
  from = aws_lb_target_group.app
  to   = module.edge.aws_lb_target_group.app
}

moved {
  from = aws_lb_listener.http
  to   = module.edge.aws_lb_listener.http
}

moved {
  from = aws_lb_target_group_attachment.app
  to   = module.edge.aws_lb_target_group_attachment.app
}

moved {
  from = aws_security_group.alb_sg
  to   = module.edge.aws_security_group.alb_sg
}

moved {
  from = aws_vpc_security_group_ingress_rule.alb_http_from_internet
  to   = module.edge.aws_vpc_security_group_ingress_rule.alb_http_from_internet
}
