# Input variables for the PlatformCore stack.
#
# Defaults are set for the current dev environment. For multi-env expansion
# (staging, prod) these will be passed via -var flags or per-env tfvars files.

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "platformcore"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"
}