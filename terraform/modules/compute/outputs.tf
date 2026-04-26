#--------------------------------------------------------------------------------------------------------
# COMPUTE MODULE OUTPUTS
#--------------------------------------------------------------------------------------------------------



output "instance_id" {
  value = aws_instance.app.id
}

output "ec2_sg_id" {
  value = aws_security_group.ec2_sg.id
}

output "iam_role_arn" {
  value = aws_iam_role.ec2_ssm.arn
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}