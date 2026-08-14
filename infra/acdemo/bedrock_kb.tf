# S3 bucket names must be globally unique. The account ID is the preferred
# suffix (stable, human-traceable, and already available here since other
# resources in this stack depend on data.aws_caller_identity.current). If
# that data source ever comes back empty -- e.g. a caller identity call that
# succeeds but returns no account ID -- fall back to a random hex suffix
# instead of a name collision. random_id has no AWS dependency of its own,
# and once created its value is fixed in state, so bucket names stay stable
# across applies either way.
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  bucket_unique_suffix = (
    data.aws_caller_identity.current.account_id != "" ?
    data.aws_caller_identity.current.account_id :
    random_id.bucket_suffix.hex
  )
}

resource "aws_iam_role" "bedrock_kb_role" {
  name        = "AmazonBedrockExecutionRoleForKnowledgeBase_acdemo-dev"
  description = "Role for the Amazon Bedrock Knowledge Base: acdemo-dev-knowledge-base"
  path        = "/service-role/"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockKBAssumeRole"
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"
          }
        }
      }
    ]

  })
}

resource "aws_iam_policy" "bedrock_kb_policy" {
  name        = "BedrockKB-Policy-acdemo-dev-knowledge-base"
  description = "Policy for the Amazon Bedrock Knowledge Base: acdemo-dev-knowledge-base to access S3"
  path        = "/service-role/"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "BedrockKBSS3Access"
        Action = [
          "s3:ListBucket",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:PutObject"
        ]
        Effect   = "Allow"
        Resource = [aws_s3_bucket.kb_data_source_bucket.arn, "${aws_s3_bucket.kb_data_source_bucket.arn}/*", aws_s3_bucket.multimodal_output_bucket.arn, "${aws_s3_bucket.multimodal_output_bucket.arn}/*"]
      },
      {
        Sid = "BedrockKBFoundationModelAccess"
        Action = [
          "bedrock:InvokeModel"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:bedrock:${var.region}::foundation-model/*"]
      },
      {
        Sid = "BedrockKBVectorIndexAccess"
        Action = [
          "s3vectors:QueryVectors",
          "s3vectors:GetVectors",
          "s3vectors:PutVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:GetIndex"
        ]
        Effect   = "Allow"
        Resource = ["*"]
      },
      {
        Sid = "BedrockKBDataAutomationAccess"
        Action = [
          "bedrock:InvokeDataAutomationAsync",
          "bedrock:GetDataAutomationStatus"
        ]
        Effect   = "Allow"
        Resource = ["*"]
      },
      {
        Sid    = "MarketplaceOperationsFromBedrockFor3pModels"
        Effect = "Allow"
        Action = [
          "aws-marketplace:Subscribe",
          "aws-marketplace:ViewSubscriptions",
          "aws-marketplace:Unsubscribe"
        ]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "aws:CalledViaLast" = "bedrock.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bedrock_kb_policy_attachment" {
  role       = aws_iam_role.bedrock_kb_role.name
  policy_arn = aws_iam_policy.bedrock_kb_policy.arn
}

# CreateKnowledgeBase checks bucket access on the role immediately, and
# nothing in aws_bedrockagent_knowledge_base's arguments references the
# policy/attachment (only role_arn, which doesn't change when the policy
# document does) -- so Terraform has no natural ordering forcing the
# attachment to land, propagate through IAM, and be visible to Bedrock
# before creation is attempted. The explicit depends_on plus a short wait
# below fixes that; without both, KB (re)creation right after a bucket
# rename can 400 with "IAM role doesn't have access to the specified bucket".
resource "time_sleep" "bedrock_kb_role_propagation" {
  depends_on      = [aws_iam_role_policy_attachment.bedrock_kb_policy_attachment]
  create_duration = "20s"
}

resource "aws_s3vectors_vector_bucket" "vector_bucket" {
  vector_bucket_name = "acdemo-dev-vector-bucket"
}

resource "aws_s3vectors_index" "vector_index" {
  index_name         = "acdemo-dev-vector-index"
  vector_bucket_name = aws_s3vectors_vector_bucket.vector_bucket.vector_bucket_name

  data_type       = "float32"
  dimension       = 1024
  distance_metric = "euclidean"

  metadata_configuration {
    non_filterable_metadata_keys = [
      "AMAZON_BEDROCK_TEXT",
      "AMAZON_BEDROCK_METADATA"
    ]
  }
}

resource "aws_s3_bucket" "multimodal_output_bucket" {
  bucket        = "acdemo-dev-multimodal-output-bucket-${local.bucket_unique_suffix}"
  force_destroy = true
}

resource "aws_s3_bucket" "kb_data_source_bucket" {
  bucket        = "acdemo-dev-source-bucket-${local.bucket_unique_suffix}"
  force_destroy = true
}


resource "aws_bedrockagent_knowledge_base" "knowledge_base" {
  depends_on  = [time_sleep.bedrock_kb_role_propagation]
  name        = "acdemo-dev-knowledge-base"
  description = "Test knowledge base for acdemo"
  role_arn    = aws_iam_role.bedrock_kb_role.arn
  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = 1024
          embedding_data_type = "FLOAT32"
        }
      }
      supplemental_data_storage_configuration {
        storage_location {
          type = "S3"

          s3_location {
            uri = "s3://${aws_s3_bucket.multimodal_output_bucket.bucket}"
          }
        }
      }
    }
  }
  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.vector_index.index_arn

    }
  }
}

resource "awscc_bedrock_data_source" "s3_data_source" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.knowledge_base.id
  name              = "acdemo-dev-s3-data-source"
  description       = "Data source for the Amazon Bedrock Knowledge Base: acdemo-dev-knowledge-base from S3 with semantic chunking"
  data_source_configuration = {
    s3_configuration = {
      bucket_arn         = aws_s3_bucket.kb_data_source_bucket.arn
      inclusion_prefixes = ["embeddings/"]
    }
    type = "S3"
  }
  vector_ingestion_configuration = {
    chunking_configuration = {
      chunking_strategy = "SEMANTIC"
      semantic_chunking_configuration = {
        breakpoint_percentile_threshold = 95
        buffer_size                     = 0 # either 0 or 1
        max_tokens                      = 300
      }
    }
    parsing_configuration = {
      parsing_strategy = "BEDROCK_DATA_AUTOMATION"
      bedrock_data_automation_configuration = {
        parsing_modality = "MULTIMODAL"
      }
    }
  }
}