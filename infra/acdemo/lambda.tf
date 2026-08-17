locals {
  feed_ingest_src                     = "${path.module}/../../src/feed_ingest"
  feed_ingest_function_name           = "acdemo-dev-feed-ingest"
  feed_ingest_scheduler_function_name = "acdemo-dev-feed-ingest-scheduler"

  pregame_report_src                     = "${path.module}/../../src/pregame_report"
  pregame_report_function_name           = "acdemo-dev-pregame-report"
  pregame_report_scheduler_function_name = "acdemo-dev-pregame-report-scheduler"
}

# --------------------------------------------------------------------------
# Deployment packages
#
# feed_ingest.zip / feed_ingest_scheduler.zip are build outputs (gitignored,
# not checked in), produced by `make feed_ingest_dist feed_ingest_scheduler_dist`
# in src/Makefile. `make plan` / `make apply` in this directory rebuild them
# before invoking Terraform -- use those instead of `terraform plan|apply`
# directly, or the two zips below can go stale against feed_ingest*.py.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# feed_ingest.py
# --------------------------------------------------------------------------

resource "aws_iam_role" "feed_ingest_role" {
  name = "${local.feed_ingest_function_name}-role"
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

resource "aws_iam_role_policy_attachment" "feed_ingest_basic_execution" {
  role       = aws_iam_role.feed_ingest_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "feed_ingest_policy" {
  name = "${local.feed_ingest_function_name}-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Documents + metadata sidecars (embeddings/) and dedup markers
        # (state/) both live under this bucket; put_object() writes both,
        # already_processed() HeadObject-checks both (HeadObject is
        # authorized via s3:GetObject, same as GetObject itself).
        Sid      = "FeedIngestS3DataAccess"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${aws_s3_bucket.kb_data_source_bucket.arn}/*"]
      },
      {
        # classify_article() calls bedrock-runtime converse() against this
        # one model (feed_ingest.py's MODEL_ID) -- scoped tightly rather
        # than to foundation-model/* since the caller is single-purpose.
        Sid      = "FeedIngestModelInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["arn:aws:bedrock:${var.region}::foundation-model/amazon.nova-lite-v1:0"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "feed_ingest_policy_attachment" {
  role       = aws_iam_role.feed_ingest_role.name
  policy_arn = aws_iam_policy.feed_ingest_policy.arn
}

resource "aws_lambda_function" "feed_ingest" {
  function_name    = local.feed_ingest_function_name
  description      = "Per-team RSS ingestion: writes documents + metadata to the KB's S3 data source"
  role             = aws_iam_role.feed_ingest_role.arn
  handler          = "feed_ingest.lambda_handler"
  runtime          = "python3.14"
  architectures    = ["arm64"]
  timeout          = 300 # 5 entries/run x (article fetch + Nova classify), well under Bedrock's Converse timeout
  memory_size      = 256 # bs4/requests HTML parsing benefits from headroom over the 128 MB default
  filename         = "${local.feed_ingest_src}/feed_ingest.zip"
  source_code_hash = filebase64sha256("${local.feed_ingest_src}/feed_ingest.zip")

  environment {
    variables = {
      DEST_BUCKET = aws_s3_bucket.kb_data_source_bucket.bucket
    }
  }
}

# --------------------------------------------------------------------------
# feed_ingest_scheduler.py
# --------------------------------------------------------------------------

resource "aws_iam_role" "feed_ingest_scheduler_role" {
  name = "${local.feed_ingest_scheduler_function_name}-role"
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

resource "aws_iam_role_policy_attachment" "feed_ingest_scheduler_basic_execution" {
  role       = aws_iam_role.feed_ingest_scheduler_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "feed_ingest_scheduler_policy" {
  name = "${local.feed_ingest_scheduler_function_name}-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # load_feed_configs() scans the whole table each run; no other
        # DynamoDB call is made.
        Sid      = "FeedIngestSchedulerReadFeedSources"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = [aws_dynamodb_table.feedsources.arn]
      },
      {
        # invoke_row() invokes feed_ingest.py synchronously, once per
        # (team, url) row.
        Sid      = "FeedIngestSchedulerInvokeFeedIngest"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.feed_ingest.arn]
      },
      {
        # maybe_start_ingestion() kicks off the KB sync once every row has
        # run and at least one document was written.
        Sid      = "FeedIngestSchedulerStartIngestion"
        Effect   = "Allow"
        Action   = ["bedrock:StartIngestionJob"]
        Resource = [aws_bedrockagent_knowledge_base.knowledge_base.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "feed_ingest_scheduler_policy_attachment" {
  role       = aws_iam_role.feed_ingest_scheduler_role.name
  policy_arn = aws_iam_policy.feed_ingest_scheduler_policy.arn
}

resource "aws_lambda_function" "feed_ingest_scheduler" {
  function_name    = local.feed_ingest_scheduler_function_name
  description      = "Runs feed_ingest.py for every configured (team, RSS feed) row, then triggers the KB sync"
  role             = aws_iam_role.feed_ingest_scheduler_role.arn
  handler          = "feed_ingest_scheduler.lambda_handler"
  runtime          = "python3.14"
  architectures    = ["arm64"]
  timeout          = 900 # 15 minutes -- runs feed_ingest.py once per row, sequentially, and waits for each
  filename         = "${local.feed_ingest_src}/feed_ingest_scheduler.zip"
  source_code_hash = filebase64sha256("${local.feed_ingest_src}/feed_ingest_scheduler.zip")

  environment {
    variables = {
      FEED_CONFIG_TABLE    = aws_dynamodb_table.feedsources.name
      FEED_INGEST_FUNCTION = aws_lambda_function.feed_ingest.function_name
      KNOWLEDGE_BASE_ID    = aws_bedrockagent_knowledge_base.knowledge_base.id
      DATA_SOURCE_ID       = awscc_bedrock_data_source.s3_data_source.data_source_id
    }
  }
}

# --------------------------------------------------------------------------
# EventBridge schedule -- fires feed_ingest_scheduler every 6 hours
# --------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "feed_ingest_scheduler_schedule" {
  name                = "${local.feed_ingest_scheduler_function_name}-schedule"
  description         = "Triggers ${local.feed_ingest_scheduler_function_name} every 6 hours"
  schedule_expression = "rate(6 hours)"
}

resource "aws_cloudwatch_event_target" "feed_ingest_scheduler_target" {
  rule = aws_cloudwatch_event_rule.feed_ingest_scheduler_schedule.name
  arn  = aws_lambda_function.feed_ingest_scheduler.arn
}

resource "aws_lambda_permission" "feed_ingest_scheduler_allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.feed_ingest_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.feed_ingest_scheduler_schedule.arn
}

# --------------------------------------------------------------------------
# Deployment packages
#
# pregame_report.zip / pregame_report_scheduler.zip are build outputs
# (gitignored, not checked in), produced by `make pregame_report_dist
# pregame_report_scheduler_dist` in src/Makefile -- both zip the handler
# module directly with no compiled requirements, since neither imports
# anything beyond boto3 (already present in the Lambda runtime), the same
# as weather_tool.py. `make plan` / `make apply` in this directory rebuild
# them before invoking Terraform -- use those instead of `terraform
# plan|apply` directly, or the two zips below can go stale against
# pregame_report*.py.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# pregame_report.py
# --------------------------------------------------------------------------

resource "aws_iam_role" "pregame_report_role" {
  name = "${local.pregame_report_function_name}-role"
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

resource "aws_iam_role_policy_attachment" "pregame_report_basic_execution" {
  role       = aws_iam_role.pregame_report_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "pregame_report_policy" {
  name = "${local.pregame_report_function_name}-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Report documents + metadata sidecars (embeddings/pregame_reports/)
        # live under this bucket -- same bucket feed_ingest.py writes into,
        # under a separate prefix.
        Sid      = "PregameReportS3DataAccess"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${aws_s3_bucket.kb_data_source_bucket.arn}/*"]
      },
      {
        # retrieve_team_context() calls bedrock-agent-runtime Retrieve
        # against this one knowledge base.
        Sid      = "PregameReportKnowledgeBaseRetrieve"
        Effect   = "Allow"
        Action   = ["bedrock:Retrieve"]
        Resource = [aws_bedrockagent_knowledge_base.knowledge_base.arn]
      },
      {
        # synthesize_report() calls bedrock-runtime converse() against this
        # one model (pregame_report.py's MODEL_ID) -- scoped tightly, same
        # as feed_ingest_policy's equivalent statement.
        Sid      = "PregameReportModelInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["arn:aws:bedrock:${var.region}::foundation-model/amazon.nova-lite-v1:0"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "pregame_report_policy_attachment" {
  role       = aws_iam_role.pregame_report_role.name
  policy_arn = aws_iam_policy.pregame_report_policy.arn
}

resource "aws_lambda_function" "pregame_report" {
  function_name    = local.pregame_report_function_name
  description      = "Per-game pregame report: retrieves team KB coverage, synthesizes a report, writes it to the KB's S3 data source"
  role             = aws_iam_role.pregame_report_role.arn
  handler          = "pregame_report.lambda_handler"
  runtime          = "python3.14"
  architectures    = ["arm64"]
  timeout          = 60 # two KB retrieves + one Nova converse call, well under Bedrock's timeout
  filename         = "${local.pregame_report_src}/pregame_report.zip"
  source_code_hash = filebase64sha256("${local.pregame_report_src}/pregame_report.zip")

  environment {
    variables = {
      DEST_BUCKET = aws_s3_bucket.kb_data_source_bucket.bucket
    }
  }
}

# --------------------------------------------------------------------------
# pregame_report_scheduler.py
# --------------------------------------------------------------------------

resource "aws_iam_role" "pregame_report_scheduler_role" {
  name = "${local.pregame_report_scheduler_function_name}-role"
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

resource "aws_iam_role_policy_attachment" "pregame_report_scheduler_basic_execution" {
  role       = aws_iam_role.pregame_report_scheduler_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "pregame_report_scheduler_policy" {
  name = "${local.pregame_report_scheduler_function_name}-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # load_game_rows() scans the whole table each run; no other
        # DynamoDB call is made.
        Sid      = "PregameReportSchedulerReadGames"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = [aws_dynamodb_table.games.arn]
      },
      {
        # invoke_game() invokes pregame_report.py synchronously, once per
        # game due within the lookahead window.
        Sid      = "PregameReportSchedulerInvokePregameReport"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.pregame_report.arn]
      },
      {
        # maybe_start_ingestion() kicks off the KB sync once every selected
        # game has run and at least one document was written. Same KB/data
        # source feed_ingest_scheduler.py's identical statement targets.
        Sid      = "PregameReportSchedulerStartIngestion"
        Effect   = "Allow"
        Action   = ["bedrock:StartIngestionJob"]
        Resource = [aws_bedrockagent_knowledge_base.knowledge_base.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "pregame_report_scheduler_policy_attachment" {
  role       = aws_iam_role.pregame_report_scheduler_role.name
  policy_arn = aws_iam_policy.pregame_report_scheduler_policy.arn
}

resource "aws_lambda_function" "pregame_report_scheduler" {
  function_name    = local.pregame_report_scheduler_function_name
  description      = "Builds/refreshes pregame reports for every game due within the lookahead window, then triggers the KB sync"
  role             = aws_iam_role.pregame_report_scheduler_role.arn
  handler          = "pregame_report_scheduler.lambda_handler"
  runtime          = "python3.14"
  architectures    = ["arm64"]
  timeout          = 300 # runs pregame_report.py once per selected game, sequentially, and waits for each
  filename         = "${local.pregame_report_src}/pregame_report_scheduler.zip"
  source_code_hash = filebase64sha256("${local.pregame_report_src}/pregame_report_scheduler.zip")

  environment {
    variables = {
      GAMES_TABLE             = aws_dynamodb_table.games.name
      PREGAME_REPORT_FUNCTION = aws_lambda_function.pregame_report.function_name
      KNOWLEDGE_BASE_ID       = aws_bedrockagent_knowledge_base.knowledge_base.id
      DATA_SOURCE_ID          = awscc_bedrock_data_source.s3_data_source.data_source_id
      PREGAME_LOOKAHEAD_DAYS  = "5"
    }
  }
}

# --------------------------------------------------------------------------
# EventBridge schedule -- fires pregame_report_scheduler every 6 hours
# --------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "pregame_report_scheduler_schedule" {
  name                = "${local.pregame_report_scheduler_function_name}-schedule"
  description         = "Triggers ${local.pregame_report_scheduler_function_name} every 6 hours"
  schedule_expression = "rate(6 hours)"
}

resource "aws_cloudwatch_event_target" "pregame_report_scheduler_target" {
  rule = aws_cloudwatch_event_rule.pregame_report_scheduler_schedule.name
  arn  = aws_lambda_function.pregame_report_scheduler.arn
}

resource "aws_lambda_permission" "pregame_report_scheduler_allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pregame_report_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.pregame_report_scheduler_schedule.arn
}
