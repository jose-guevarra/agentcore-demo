terraform {
  required_version = ">= 1.14.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.28.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.68.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.profile != "" ? var.profile : null
  default_tags {
    tags = var.tags
  }
}

provider "awscc" {
  region  = var.region
  profile = var.profile != "" ? var.profile : null
}