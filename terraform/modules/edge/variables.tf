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

variable "zone_id" {
  type = string
  description = "Route 53 zone id for domain"
  default = ""
}

variable "domain_name" {
  type = string
  description = "Domain name for the app"
  default = ""
}

variable "create_https" {
  description = "Whether to create Route 53, ACM, and HTTPS listener resources"
  type        = bool
  default     = false
}