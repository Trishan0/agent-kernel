output "agent_invoke_url" {
  description = "Base URL. POST .../api/v1/chat to run a sweep; the Telegram webhook is .../api/v1/telegram/webhook"
  value       = module.serverless_agents.agent_invoke_url
}

output "telegram_webhook_url" {
  description = "Register this with the Telegram Bot API setWebhook"
  value       = "${module.serverless_agents.agent_invoke_url}/api/v1/telegram/webhook"
}

output "schedule_group_name" {
  description = "EventBridge Scheduler schedule-group each task registers in"
  value       = module.serverless_agents.schedule_group_name
}

output "schedule_table_name" {
  description = "DynamoDB schedule (task) store table"
  value       = module.serverless_agents.schedule_table_name
}

output "site_state_table_name" {
  description = "DynamoDB site state store (baselines, dismissals, cases), partition key site_id"
  value       = aws_dynamodb_table.site_state.name
}
