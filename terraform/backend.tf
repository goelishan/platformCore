terraform {
    backend "s3" {
        bucket="platformcore-tf-state"
        key="platformcore/terraform.tfstate"
        region="us-east-1"
        dynamodb_table="platformcore-tf-locks"
        encrypt=true
    }
}