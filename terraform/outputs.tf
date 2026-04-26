# Stack outputs - the public contract for downstream consumers, humans, and
# test scripts. Anything exposed here is the stable interface; internals
# (route table IDs, IGW ID, SG rule IDs, etc.) are deliberately NOT surfaced
# so consumers can't couple to them.

output "account_id" {
  description = "AWS account ID running this stack"
  value       = data.aws_caller_identity.current.account_id
}

output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "IDs of the public subnets (ALB lives here)"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets (EC2 / RDS live here)"
  value       = aws_subnet.private[*].id
}

output "app_instance_id" {
  description = "Instance ID of the app EC2. Use with: aws ssm start-session --target <id>"
  value       = aws_instance.app.id
}

output "ec2_ssm_role_arn" {
  description = "ARN of the IAM role attached to the app EC2 via its instance profile"
  value       = aws_iam_role.ec2_ssm.arn
}

output "alb_dns_name" {
  description = "Public DNS name of the ALB"
  value       = aws_lb.app.dns_name
}

output "alb_url" {
  description = "Full HTTP URL to the app via the ALB (curl this to smoke-test)"
  value       = "http://${aws_lb.app.dns_name}"
}

output "rds_endpoint" {
  description = "Postgres endpoint (hostname:port). Reachable only from EC2 SG."
  value       = aws_db_instance.main.endpoint
}