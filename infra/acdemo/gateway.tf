locals {
  weather_tool_src           = "${path.module}/../../src/weather_tool"
  weather_tool_function_name = "acdemo-dev-weather-tool"
  weather_gateway_name       = "acdemo-dev-weather-gateway"
}

# --------------------------------------------------------------------------
# Deployment package
#
# weather_tool.zip is a build output (gitignored, not checked in), produced
# by `make weather_tool_dist` in src/Makefile. `make plan` / `make apply` in
# this directory rebuild it before invoking Terraform -- use those instead of
# `terraform plan|apply` directly, or the zip below can go stale against
# weather_tool.py.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# weather_tool.py -- the Lambda the Gateway target below invokes
# --------------------------------------------------------------------------

resource "aws_iam_role" "weather_tool_role" {
  name = "${local.weather_tool_function_name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaAssumeRole"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "weather_tool_basic_execution" {
  role       = aws_iam_role.weather_tool_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# No custom policy beyond basic execution -- weather_tool.py only makes
# outbound HTTPS calls to Open-Meteo's public API, no other AWS API calls.

resource "aws_lambda_function" "weather_tool" {
  function_name    = local.weather_tool_function_name
  description      = "Backs the get_weather MCP tool: geocodes a city and fetches current conditions from Open-Meteo"
  role             = aws_iam_role.weather_tool_role.arn
  handler          = "weather_tool.lambda_handler"
  runtime          = "python3.14"
  architectures    = ["arm64"]
  timeout          = 10 # two HTTPS round-trips (geocode + forecast), no AWS API calls
  filename         = "${local.weather_tool_src}/weather_tool.zip"
  source_code_hash = filebase64sha256("${local.weather_tool_src}/weather_tool.zip")
}

# --------------------------------------------------------------------------
# Gateway -- exposes weather_tool.py as an MCP tool over HTTP, guarded by the
# same Cognito user pool/client the AgentCore Runtime itself already trusts
# (agentcore_runtime.tf), so only requests bearing a valid access token for
# that pool/client reach the Lambda.
# --------------------------------------------------------------------------

resource "aws_iam_role" "weather_gateway_role" {
  name = "${local.weather_gateway_name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "BedrockAgentCoreAssumeRole"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
    }]
  })
}

resource "aws_iam_policy" "weather_gateway_policy" {
  name = "${local.weather_gateway_name}-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The Gateway target below authenticates to weather_tool.py via
        # GATEWAY_IAM_ROLE (this role calling lambda:InvokeFunction directly),
        # not a resource-based Lambda permission.
        Sid      = "WeatherGatewayInvokeWeatherTool"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.weather_tool.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "weather_gateway_policy_attachment" {
  role       = aws_iam_role.weather_gateway_role.name
  policy_arn = aws_iam_policy.weather_gateway_policy.arn
}

resource "aws_bedrockagentcore_gateway" "weather_gateway" {
  name            = local.weather_gateway_name
  description     = "MCP gateway exposing the get_weather tool to the acdemo-dev chat agent"
  role_arn        = aws_iam_role.weather_gateway_role.arn
  authorizer_type = "CUSTOM_JWT"
  protocol_type   = "MCP"

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.userpool.id}/.well-known/openid-configuration"
      allowed_clients = [aws_cognito_user_pool_client.userpool_client.id]
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "weather_target" {
  gateway_identifier = aws_bedrockagentcore_gateway.weather_gateway.gateway_id
  name               = "get-weather"
  description        = "Routes the get_weather MCP tool call to weather_tool.py"

  credential_provider_configuration {
    # The Gateway invokes the Lambda directly via its own execution role
    # (weather_gateway_role above), not SigV4/OAuth/API-key -- same identity-
    # policy-only pattern feed_ingest_scheduler already uses to invoke
    # feed_ingest.py (see lambda.tf).
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.weather_tool.arn

        tool_schema {
          inline_payload {
            name        = "get_weather"
            description = "Get the current weather conditions for a city."

            input_schema {
              type = "object"

              property {
                name        = "city"
                type        = "string"
                description = "Name of the city to look up weather for, for example Paris or Austin."
                required    = true
              }
            }
          }
        }
      }
    }
  }
}
