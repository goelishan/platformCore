#--------------------------------------------------------------------------------------------------------
# RDS POSTGRES
#--------------------------------------------------------------------------------------------------------

#   db_subnet_group — placement domain.

resource "aws_db_subnet_group" "main" {
    name="${var.project_name}-db-subnet-group"
    subnet_ids = aws_subnet.private[*].id

    tags = {
        Name="${var.project_name}-db-subnet-group"
        Environment=var.environment
    }
}
#   security_group + ingress rule  

resource "aws_security_group" "rds_sg" {
    name="${var.project_name}-rds-sg"
    description = "Postgres ingress from app only"
    vpc_id = aws_vpc.main.id

    tags = {
        Name="${var.project_name}-rds-sg"
        Environment=var.environment
    }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_ec2" {
  security_group_id = aws_security_group.rds_sg.id
  referenced_security_group_id = aws_security_group.ec2_sg.id
  ip_protocol = "tcp"
  from_port = 5432
  to_port = 5432
  description = "Postgres from app ec2 SG"
}


#   db_parameter_group  

resource "aws_db_parameter_group" "main" {
    name = "${var.project_name}-pg17"
    family = "postgres17"

    tags = {
        Name="${var.project_name}-pg17"
        Environment=var.environment
    }
}

#   random_password

resource "random_password" "rds_master" {
  length=16
  special = false
}

#   db_instance 

resource "aws_db_instance" "main" {
    identifier = "${var.project_name}-db"
    engine = "postgres"
    engine_version = 17


    instance_class = "db.t3.micro"
    allocated_storage = 20
    storage_type = "gp3"
    storage_encrypted = true

    db_name="platformcore"
    username = "platformcore"
    password = random_password.rds_master.result

    db_subnet_group_name = aws_db_subnet_group.main.name
    vpc_security_group_ids = [aws_security_group.rds_sg.id]
    parameter_group_name = aws_db_parameter_group.main.name

    publicly_accessible = false
    multi_az = false

    backup_retention_period = 0
    skip_final_snapshot = true
    deletion_protection = false
    apply_immediately = true

    tags = {
        Name="${var.project_name}-db"
        Environment=var.environment
    }

}