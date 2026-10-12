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
  description = "RDS resource ID. Used to scope rds-db:connect to this instance only."
  type        = string
}

variable "oncall_namespace" {
  description = "Namespace of the on-call agent. Also excluded from its own collection."
  type        = string
  default     = "oncall"
}

variable "oncall_service_account" {
  description = "ServiceAccount the on-call agent runs as; the only principal its role trusts."
  type        = string
  default     = "oncall-agent"
}

variable "oncall_bedrock_inference_profile" {
  description = "Inference profile the agent calls. Must match ONCALL_BEDROCK_MODEL_ID."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "oncall_bedrock_foundation_model" {
  description = "Foundation model behind the profile, as named in its foundation-model ARNs."
  type        = string
  default     = "anthropic.claude-sonnet-4-6"
}

variable "oncall_bedrock_guardrail_arn" {
  description = "Bedrock guardrail the agent applies, if any. Empty grants no ApplyGuardrail."
  type        = string
  default     = ""
}

variable "oncall_bedrock_model_regions" {
  description = "Every region the profile routes to, from aws bedrock get-inference-profile."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}
