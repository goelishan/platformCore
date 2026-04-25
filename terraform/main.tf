# Shared data sources used across multiple files.
#
# Kept here (rather than nested next to any specific resource) because these
# don't belong to a single concern - aws_caller_identity is referenced by
# outputs.tf for the account_id output and may be used by future IAM resources.

data "aws_caller_identity" "current" {}

