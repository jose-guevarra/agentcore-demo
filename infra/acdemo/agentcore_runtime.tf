resource "aws_ecr_repository" "agentcore_runtime_agent_code_ecr_repository" {
  name         = "acdemo-dev-runtime-agent-code-ecr-repository"
  force_delete = true
}

resource "null_resource" "push_initial_image" {
  // This makes an initial push to the ECR repository for AgentCore Runtime to be created
  depends_on = [aws_ecr_repository.agentcore_runtime_agent_code_ecr_repository]

  triggers = {
    repository_url = aws_ecr_repository.agentcore_runtime_agent_code_ecr_repository.repository_url
    region         = var.region
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      REPO_URL="${aws_ecr_repository.agentcore_runtime_agent_code_ecr_repository.repository_url}"
      REPO_NAME="${aws_ecr_repository.agentcore_runtime_agent_code_ecr_repository.name}"
      REGION="${var.region}"
      
      echo "Checking if image already exists in ECR..."
      if aws ecr describe-images --repository-name $REPO_NAME --image-ids imageTag=latest --region $REGION 2>/dev/null | grep -q "imageTags"; then
        echo "Image with tag 'latest' already exists in ECR. Skipping push."
        exit 0
      fi
      
      echo "Image not found. Pushing initial image..."
      echo "Authenticating with ECR..."
      aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REPO_URL
      
      echo "Pulling alpine image..."
      docker pull alpine:latest
      
      echo "Tagging image for ECR..."
      docker tag alpine:latest $REPO_URL:latest
      
      echo "Pushing image to ECR..."
      docker push $REPO_URL:latest
      
      echo "Image pushed successfully!"
    EOT
  }
}

resource "aws_iam_role" "agentcore_runtime_role" {
  name = "acdemo-dev-runtime-role"
  path = "/service-role/"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockAgentCoreAssumeRole"
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_policy" "agentcore_runtime_execution_policy" {
  name        = "acdemo-dev-runtime-execution-policy"
  path        = "/service-role/"
  description = "Policy for the Amazon Bedrock AgentCore Runtime: acdemo-dev-runtime"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "ECRImageAccess"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/*"]
      },
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:DescribeLogStreams"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
      },
      {
        Action = [
          "logs:DescribeLogGroups"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
      },
      {
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
      },
      {
        Action = [
          "ecr:GetAuthorizationToken"
        ]
        Effect   = "Allow"
        Resource = ["*"]
      },
      {
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Effect   = "Allow"
        Resource = ["*"]
      },
      {
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Effect   = "Allow"
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "bedrock-agentcore"
          }
        }
      },
      {
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
        ]
        Effect = "Allow"
        Resource = [
          "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/acdemo-dev-runtime-*"
        ]
      },
      {
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Retrieve",
          "bedrock:ApplyGuardrail"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
      },
      {
        Action = [
          "aws-marketplace:Subscribe",
          "aws-marketplace:ViewSubscriptions",
          "aws-marketplace:Unsubscribe"
        ]
        Effect   = "Allow"
        Resource = ["*"]
      },
      {
        Action = [
          "s3:GetObject"
        ]
        Effect   = "Allow"
        Resource = ["arn:aws:s3:::*"]
      },
      {
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:DeleteEvent",
          "bedrock-agentcore:ListSessions"
        ]
        Effect   = "Allow"
        Resource = [aws_bedrockagentcore_memory.chat_memory.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "agentcore_runtime_execution_policy_attachment" {
  role       = aws_iam_role.agentcore_runtime_role.name
  policy_arn = aws_iam_policy.agentcore_runtime_execution_policy.arn
}

resource "aws_bedrockagentcore_memory" "chat_memory" {
  name                  = "acdemo_dev_chat_memory"
  description           = "Short-term conversational memory for the acdemo-dev chat agent, keyed by user."
  event_expiry_duration = 7 # days; provider enforces a 7-365 range (lowest it accepts)
}


resource "aws_bedrockagentcore_agent_runtime" "agentcore_runtime" {
  agent_runtime_name = "acdemo_dev_runtime"
  description        = "Agentcore runtime for the acdemo-dev application"
  role_arn           = aws_iam_role.agentcore_runtime_role.arn
  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = {
    BEDROCK_KNOWLEDGE_BASE_ID = aws_bedrockagent_knowledge_base.knowledge_base.id
    BEDROCK_MEMORY_ID         = aws_bedrockagentcore_memory.chat_memory.id
  }

  # Both fields are set explicitly (rather than only max_lifetime) to avoid a
  # known terraform-provider-aws issue where omitting one causes an
  # "inconsistent result after apply" error once AWS computes its default.
  # idle_runtime_session_timeout is left at AWS's own default (900s/15min);
  # max_lifetime is capped to 1 hour so no runtime instance lives past that.
  lifecycle_configuration = [{
    idle_runtime_session_timeout = 900
    max_lifetime                 = 3600
  }]

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.userpool.id}/.well-known/openid-configuration"
      allowed_clients = [aws_cognito_user_pool_client.userpool_client.id]
    }
  }

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agentcore_runtime_agent_code_ecr_repository.repository_url}:latest"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  depends_on = [null_resource.push_initial_image]
}
