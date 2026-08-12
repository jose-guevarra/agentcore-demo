variable "tags" {
  type        = map(string)
  description = "Tags to apply to all resources"
  default = {
    project = "acdemo-dev"
  }
}

variable "region" {
  type        = string
  description = "AWS region"
  default     = "us-east-1"
}

variable "profile" {
  type        = string
  description = "AWS profile"
  default     = ""
}

variable "data_source_bucket_arn" {
  type        = string
  description = "The ARN of the S3 bucket to source the knowledge base"
}

variable "ecr_repository_name" {
  type        = string
  description = "The name of the ECR repository to store the agent code"
}