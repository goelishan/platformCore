#--------------------------------------------------------------------------------------------------------
# COMPUTE MODULE INPUTS
#--------------------------------------------------------------------------------------------------------



variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "EC2 placement subnet — module picks index [0] for single-instance deploy."
}

variable "aws_region" {
  type        = string
  description = "Used for ECR endpoint URLs in user_data and IAM policy ARN scoping."
}

variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

# DB connection inputs from the data module — flow through to user_data.
variable "db_endpoint" {
  type = string
}

variable "db_username" {
  type = string
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "db_name" {
  type = string
}