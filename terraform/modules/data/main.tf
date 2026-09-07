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
# Eligibility is assessed on the major version, so the full minor pin below
# narrows what gets created without touching that constraint.



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
  name                    = "${var.project_name}/db/master"
  description             = "RDS master credential for ${var.project_name}"
  recovery_window_in_days = 0

  tags = {
    Name        = "${var.project_name}-db-master-secret"
    Environment = var.environment
  }
}

resource "aws_secretsmanager_secret_version" "db_master" {
  secret_id = aws_secretsmanager_secret.db_master.id
  secret_string = jsonencode({
    username = aws_db_instance.main.username
    password = random_password.rds_master.result
  })
}

resource "aws_secretsmanager_secret" "nginx_api_key" {
  name                    = "${var.project_name}/app/nginx-api-key"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "nginx_api_key" {
  secret_id     = aws_secretsmanager_secret.nginx_api_key.id
  secret_string = jsonencode({ API_KEY = "placeholder-api-key" })
}

resource "aws_secretsmanager_secret" "postgres_app_password" {
  name                    = "${var.project_name}/app/postgres-password"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "postgres_app_password" {
  secret_id     = aws_secretsmanager_secret.postgres_app_password.id
  secret_string = jsonencode({ POSTGRES_PASSWORD = "platformcore" })
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
#
# Why the engine version is fully qualified: engine_version = "17" lets RDS pick
# the minor at create time, so two applies of the same commit can produce different
# database versions. 17.11 is the newest minor offered in us-east-1 at the time of
# this change, so the pin freezes the version forward rather than stranding the
# instance on an older patch than it would otherwise have received.
#
# Why auto_minor_version_upgrade is false: the argument defaults to TRUE. Until
# now AWS could move the engine during the maintenance window with nothing changing
# in this repository - the one upgrade path in the platform that no diff described
# and no revert could undo. It is off so that an engine version change is a commit.
#
# What that costs: minor releases carry security fixes, and they now wait for a
# human. The bump is engine_version above, reviewed like any other line. AWS still
# forces the version at end of standard support for Postgres 17, so this defers the
# upgrade rather than declining it - and backup_retention_period = 0 means there is
# no automated restore point on the day it lands. That is the flip-list item most
# coupled to this decision, not an independent one.
#
# What a forced upgrade looks like here: AWS moves the instance to, say, 17.12, the
# next plan proposes returning it to the pinned 17.11, and RDS rejects downgrades, so
# the apply fails. That is the intended behaviour and not a gap to paper over. The
# alternative, lifecycle { ignore_changes = [engine_version] }, would silence the
# diff and leave the file describing a version the instance no longer runs, which is
# the failure this pin was added to end. The recovery is one line: read the running
# version, set it here, apply.
#
# apply_immediately = true above means a version bump made from this repository
# reboots the instance when it is applied rather than waiting for the window below.
# The window governs what AWS initiates, not what terraform does; on a learning
# environment that is the wanted behaviour, and on the production flip-list it turns
# off with the other three.
#
# Why the maintenance window is explicit: disabling minor upgrades does not empty
# the window. AWS still applies required patching and hardware maintenance in it,
# and an unset window is assigned at random within a region-wide block. Sunday
# 06:00-06:30 UTC is 11:30 IST on a weekend. No backup window is set to pair with
# it because retention is 0; the two are set together or not at all. RDS assigns a
# backup window regardless and refuses one that overlaps the maintenance window, but
# only when both are named explicitly: leaving it unset lets RDS pick a
# non-overlapping slot itself, which is one fewer number to keep true here.



resource "aws_db_instance" "main" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = "17.11"

  auto_minor_version_upgrade = false
  maintenance_window         = "sun:06:00-sun:06:30"

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

  iam_database_authentication_enabled = true

  tags = {
    Name        = "${var.project_name}-db"
    Environment = var.environment
  }
}
