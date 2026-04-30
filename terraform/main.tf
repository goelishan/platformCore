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

locals {
  environment=terraform.workspace=="default" ? "dev" : terraform.workspace
}

#--------------------------------------------------------------------------------------------------------
# NETWORK MODULE
#--------------------------------------------------------------------------------------------------------



module "network" {
  source = "./modules/network"

  project_name = var.project_name
  environment  = local.environment
  aws_region   = var.aws_region
  vpc_cidr     = var.vpc_cidr
}

#--------------------------------------------------------------------------------------------------------
# DATA MODULE
#--------------------------------------------------------------------------------------------------------



module "data" {
  source = "./modules/data"

  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  project_name       = var.project_name
  environment        = local.environment
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
  environment        = local.environment

  db_endpoint    = module.data.endpoint
  db_username    = module.data.username
  db_secret_name = module.data.db_secret_name
  db_name        = module.data.db_name
  db_secret_arn  = module.data.db_secret_arn
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
  environment       = local.environment
  zone_id           = var.zone_id
  domain_name       = var.domain_name
  create_https      = var.create_https
}