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
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "IDs of the public subnets (ALB lives here)"
  value       = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  description = "IDs of the private subnets (EC2 / RDS live here)"
  value       = module.network.private_subnet_ids
}

output "app_instance_id" {
  description = "Instance ID of the app EC2. Use with: aws ssm start-session --target <id>"
  value       = module.compute.instance_id
}

output "ec2_ssm_role_arn" {
  description = "ARN of the IAM role attached to the app EC2 via its instance profile"
  value       = module.compute.iam_role_arn
}

output "ecr_repository_url" {
  description = "ECR repository URL for the app image. Used by docker push during deploy."
  value       = module.compute.ecr_repository_url
}

output "alb_dns_name" {
  description = "Public DNS name of the ALB"
  value       = module.edge.alb_dns_name
}

output "alb_url" {
  description = "Full HTTP URL to the app via the ALB (curl this to smoke-test)"
  value       = module.edge.alb_url
}

output "rds_endpoint" {
  description = "Postgres endpoint (hostname:port). Reachable only from EC2 SG."
  value       = module.data.endpoint
}
