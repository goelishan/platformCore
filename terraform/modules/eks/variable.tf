#--------------------------------------------------------------------------------------------------------
# VARIABLES - EKS MODULE
#--------------------------------------------------------------------------------------------------------
terraform {
  required_providers {
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }

    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }

    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
}

variable "project_name" {
  description = "Project name used as a prefix for all resource names"
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the node group and cluster VPC config"
  type        = list(string)
}

variable "admin_iam_arn" {
  description = "IAM user or role ARN to grant cluster-admin access via Access Entry"
  type        = string
}

variable "db_resource_id" {
  description = "RDS resource ID (db-XXXXX). Used to scope rds-db:connect to this instance only."
  type        = string
}