#--------------------------------------------------------------------------------------------------------
# DATA MODULE
#--------------------------------------------------------------------------------------------------------
#
# Managed Postgres for app's persistent state. Five resources + random_password,
# each encoding a separate concern: subnet group (placement), security group
# (auth - rules cross-module at root), parameter group (DB tuning), random_password
# (master credential), db_instance (the actual DB).
#
# Engine pinned to Postgres 17 - free-tier eligibility constraint as of
# 2026-04-25 (only 17 is eligible on db.t3.micro). Free Tier is a tuple
# constraint on (instance_class, engine, engine_version, region); pinning any
# axis independently produces silent breakage on the next AWS rotation.



#--------------------------------------------------------------------------------------------------------
# SUBNET GROUP
#--------------------------------------------------------------------------------------------------------
#
# Spans both private AZs even on single-AZ deployment. Multi-AZ becomes a
# flag flip on the db_instance, not a recreation. Required by RDS even for
# single-AZ - it tells RDS where it CAN place the instance.



resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-db-subnet-group"
  subnet_ids = var.private_subnet_ids

  tags = {
    Name        = "${var.project_name}-db-subnet-group"
    Environment = var.environment
  }
}



#--------------------------------------------------------------------------------------------------------
# PARAMETER GROUP
#--------------------------------------------------------------------------------------------------------
#
# Custom, family=postgres17. Empty body today; future tuning (max_connections,
# work_mem, log_statement) lands here in place, no DB recreation. The default
# group (default.postgres17) is AWS-managed and can't be edited - always own
# a custom group from day one.



resource "aws_db_parameter_group" "main" {
  name   = "${var.project_name}-pg17"
  family = "postgres17"

  tags = {
    Name        = "${var.project_name}-pg17"
    Environment = var.environment
  }
}



#--------------------------------------------------------------------------------------------------------
# SECURITY GROUP
#--------------------------------------------------------------------------------------------------------
#
# rds_sg owns its identity. The ingress rule (5432 from ec2_sg) lives
# cross-module at root - same circular-module-dependency-break pattern as
# Day 8's standalone SG rules.



resource "aws_security_group" "rds_sg" {
  name        = "${var.project_name}-rds-sg"
  description = "Postgres ingress from app only"
  vpc_id      = var.vpc_id

  tags = {
    Name        = "${var.project_name}-rds-sg"
    Environment = var.environment
  }
}



#--------------------------------------------------------------------------------------------------------
# RANDOM PASSWORD
#--------------------------------------------------------------------------------------------------------
#
# 16 char alphanumeric (special=false to avoid URL-encoding hassles in
# DATABASE_URL). Day 12 tech debt - production replaces this with AWS
# Secrets Manager + a data source lookup at apply time + a VPC endpoint
# for secretsmanager. The password currently flows out as a sensitive
# output, into compute module's user_data, where it's visible to anyone
# with ec2:DescribeInstanceAttribute.



resource "random_password" "rds_master" {
  length  = 16
  special = false
}

#--------------------------------------------------------------------------------------------------------
# SECRETS MANAGER
#--------------------------------------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "db_master" {
  name="${var.project_name}/db/master"
  description="RDS master credential for ${var.project_name}"
  recovery_window_in_days = 0

  tags={
    Name="${var.project_name}-db-master-secret"
    Environment=var.environment
  }
}

resource "aws_secretsmanager_secret_version" "db_master" {
  secret_id = aws_secretsmanager_secret.db_master.id
  secret_string = jsonencode({
    username=aws_db_instance.main.username
    password=random_password.rds_master.result
  })
}

#--------------------------------------------------------------------------------------------------------
# DB INSTANCE
#--------------------------------------------------------------------------------------------------------
#
# db.t3.micro, gp3 20 GB, encrypted at rest, single-AZ, not publicly
# accessible. Lifecycle flags tuned for a learning env; production flip-list:
#   skip_final_snapshot     = false  + final_snapshot_identifier = "..."
#   deletion_protection     = true
#   backup_retention_period = 7..35
#   apply_immediately       = false  (defer to maintenance window)



resource "aws_db_instance" "main" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = "17"

  instance_class    = "db.t3.micro"
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "platformcore"
  username = "platformcore"
  password = random_password.rds_master.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds_sg.id]
  parameter_group_name   = aws_db_parameter_group.main.name

  publicly_accessible = false
  multi_az            = false

  backup_retention_period = 0
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  tags = {
    Name        = "${var.project_name}-db"
    Environment = var.environment
  }
}
