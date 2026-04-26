#--------------------------------------------------------------------------------------------------------
# DATA MODULE INPUTS
#--------------------------------------------------------------------------------------------------------



variable "vpc_id" {
  type        = string
  description = "VPC the rds_sg lives in. Sourced from the network module."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets across both AZs. RDS subnet group spans them so Multi-AZ becomes a flag flip."
}

variable "project_name" {
  type        = string
  description = "Used for resource naming."
}

variable "environment" {
  type        = string
  description = "Environment label (dev/staging/prod)."
}