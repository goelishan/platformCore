# Provider configuration + required versions.
#
# default_tags propagates Project + ManagedBy to every AWS resource without
# per-resource boilerplate. Name and Environment stay explicit per resource
# (Name must be unique per resource and can't sensibly be defaulted;
# Environment stays explicit so an interviewer scanning any one file sees
# the full tagset at the resource level).

terraform {
  required_version = ">=1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}

