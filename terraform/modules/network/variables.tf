variable "project_name" {
  type        = string
  description = "Used for resource naming and tagging."
}

variable "environment" {
  type        = string
  description = "Environment label (dev/staging/prod)."
}

variable "aws_region" {
  type        = string
  description = "Used by VPC endpoint service names."
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}