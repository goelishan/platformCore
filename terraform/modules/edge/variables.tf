#--------------------------------------------------------------------------------------------------------
# EDGE MODULE INPUTS
#--------------------------------------------------------------------------------------------------------



variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Public subnets across both AZs. ALB attaches to these so it's multi-AZ by construction."
}

variable "instance_id" {
  type        = string
  description = "EC2 instance ID for target group attachment. Sourced from compute module after that migration; from root before."
}

variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}