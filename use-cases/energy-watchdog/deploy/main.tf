# ---------------------------------------------------------------------------
# Energy Anomaly Watchdog - AWS serverless deployment
#
# queue_mode + EventBridge Scheduler + DynamoDB stores. Structure follows
# examples/aws-serverless/schedule-openai/deploy. NOT verified against a live AWS account -
# see deploy/README.md for what the operator must supply.
# ---------------------------------------------------------------------------

module "serverless_agents" {
  source  = "yaalalabs/ak-serverless/aws"
  version = "0.8.1"

  providers            = { aws = aws, docker = docker }
  product_alias        = var.product_alias
  env_alias            = var.env_alias
  module_name          = var.module_name
  product_display_name = "Energy Anomaly Watchdog"
  region               = var.region
  is_production        = var.is_production
  vpc_id               = var.vpc_id
  private_subnet_ids   = var.private_subnet_ids

  # ---- Queue mode: mandatory for scheduling ----
  queue_mode     = true
  execution_mode = "rest_sync"

  # ---- Stores ----
  create_dynamodb_memory_table   = true
  create_dynamodb_response_store = true

  # ---- Scheduling: EventBridge Scheduler group + role + DynamoDB task store ----
  enable_scheduling              = true
  create_dynamodb_schedule_table = true

  # ---- API Gateway ----
  api_version    = "v1"
  api_base_path  = "api"
  agent_endpoint = "chat"

  # Exact-match paths; they mirror the Lambda.register routes in lambda.py.
  gateway_endpoints = [
    { path = "telegram/webhook", method = "POST" },
    { path = "schedules", method = "GET" },
    { path = "schedules/get", method = "GET" },
    { path = "schedules/amend", method = "POST" },
    { path = "schedules/cancel", method = "POST" },
  ]

  request_handler = {
    module_name         = "rqst-hdlr"
    function_name       = "rqh-func"
    function_description = "Chat ingress, Telegram webhook and scheduled-task management"
    handler_path        = "lambda.handler"
    package_type        = "LocalZip"
    package_path        = "../dist_request_handler.zip"
    memory_size         = 256
    timeout             = 45
    environment_variables = {
      "GEMINI_API_KEY"                 = var.gemini_api_key
      "AK_AGENT_MODEL"                 = var.agent_model
      "AK_TELEGRAM__BOT_TOKEN"         = var.telegram_bot_token
      "AK_TELEGRAM__WEBHOOK_SECRET"    = var.telegram_webhook_secret
      "AK_STATE__BACKEND"              = "dynamodb"
      "AK_STATE__DYNAMODB__TABLE_NAME" = aws_dynamodb_table.site_state.name
    }
  }

  agent_runner = {
    module_name         = "agent-runner"
    function_name       = "ar-func"
    function_description = "Runs sweep + chase occurrences, hosts create_schedule"
    handler_path        = "lambda_agent_runner.handler"
    package_type        = "Image"
    package_path        = "../dist_agent_runner"
    timeout             = 120
    memory_size         = 1024
    environment_variables = {
      "GEMINI_API_KEY"                 = var.gemini_api_key
      "AK_AGENT_MODEL"                 = var.agent_model
      "AK_TELEGRAM__BOT_TOKEN"         = var.telegram_bot_token
      "AK_STATE__BACKEND"              = "dynamodb"
      "AK_STATE__DYNAMODB__TABLE_NAME" = aws_dynamodb_table.site_state.name
    }
  }

  response_handler = {
    module_name         = "rspns-hdlr"
    function_name       = "rsh-func"
    function_description = "Writes completed responses to the response store"
    handler_path        = "lambda_response_handler.handler"
    package_type        = "LocalZip"
    package_path        = "../dist_response_handler.zip"
    memory_size         = 256
    timeout             = 45
  }

  queue_config = {
    input_queue_visibility_timeout        = 130
    input_queue_max_receive_count         = 3
    input_queue_create_dlq                = true
    input_queue_message_retention_seconds = 3600

    output_queue_visibility_timeout        = 60
    output_queue_max_receive_count         = 3
    output_queue_create_dlq                = true
    output_queue_message_retention_seconds = 3600

    batch_size                         = 10
    maximum_batching_window_in_seconds = 0
  }
}

# ---- Site state store: one item per site, partition key site_id ----
resource "aws_dynamodb_table" "site_state" {
  name         = "${var.product_alias}-${var.env_alias}-site-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "site_id"

  attribute {
    name = "site_id"
    type = "S"
  }
}

# The request-handler and agent-runner execution roles need GetItem/PutItem on the table above.
# The role identifiers exposed by the ak-serverless module vary by version; attach a policy such
# as the following once you know them (see deploy/README.md):
#
# resource "aws_iam_role_policy" "state_access_agent_runner" {
#   role   = module.serverless_agents.agent_runner_role_name
#   policy = data.aws_iam_policy_document.site_state_rw.json
# }
data "aws_iam_policy_document" "site_state_rw" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.site_state.arn]
  }
}
