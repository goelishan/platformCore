# Remote state backend: S3 for storage + DynamoDB for locking.
#
# Why S3: durable, versioned (stolen/deleted state is recoverable), encrypted
# at rest, shared across the team - no laptop-local state file to lose.
# Why DynamoDB: S3 alone does NOT provide locking. Two concurrent applies
# would both read the same state, both modify cloud, both write - last writer
# wins, intermediate resources orphaned. DynamoDB's conditional writes give
# us the pessimistic lock that prevents that race.

terraform {
  backend "s3" {
    bucket         = "platformcore-tf-state"
    key            = "platformcore/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "platformcore-tf-locks"
    encrypt        = true
  }
}