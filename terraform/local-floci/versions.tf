terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "local-only"
  secret_key                  = "local-only"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  skip_region_validation      = true
  s3_use_path_style           = true

  endpoints {
    ec2   = var.floci_endpoint
    efs   = var.floci_endpoint
    elbv2 = var.floci_endpoint
    iam   = var.floci_endpoint
    rds   = var.floci_endpoint
    sts   = var.floci_endpoint
  }
}
