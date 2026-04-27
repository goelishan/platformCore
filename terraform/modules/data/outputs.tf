#--------------------------------------------------------------------------------------------------------
# DATA MODULE OUTPUTS
#--------------------------------------------------------------------------------------------------------
#
# Password is marked sensitive so it propagates the redaction flag through any
# parent that references it. user_data interpolation in compute module will pick
# up the sensitive marker automatically — same propagation chain as Day 10.



output "endpoint" {
  value       = aws_db_instance.main.endpoint
  description = "hostname:port for connection strings."
}

output "username" {
  value = aws_db_instance.main.username
}

output "db_name" {
  value = aws_db_instance.main.db_name
}

output "password" {
  value     = random_password.rds_master.result
  sensitive = true
}

output "rds_sg_id" {
  value = aws_security_group.rds_sg.id
}

output "db_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the DB master credential."
  value       = aws_secretsmanager_secret.db_master.arn
}

output "db_secret_name" {
  description = "Name of the secrets manager secret"
  value = aws_secretsmanager_secret.db_master.name
}