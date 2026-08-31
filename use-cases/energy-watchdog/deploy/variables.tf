variable "region" {
  type        = string
  description = "AWS region"
}

variable "product_alias" {
  type        = string
  description = "Product alias (prefix for resource names)"
}

variable "env_alias" {
  type        = string
  description = "Environment alias (dev / stg / prod)"
}

variable "module_name" {
  type        = string
  description = "Module name"
}

variable "is_production" {
  type        = bool
  default     = false
  description = "Is production"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID for the Lambda functions"
}

variable "private_subnet_ids" {
  type        = list(string)
  sensitive   = true
  description = "Private subnet IDs for the Lambda functions"
}

variable "gemini_api_key" {
  type        = string
  sensitive   = true
  description = "Gemini API key for the agents"
}

variable "agent_model" {
  type        = string
  default     = "gemini-2.0-flash"
  description = "Model name for the agents (AK_AGENT_MODEL)"
}

variable "telegram_bot_token" {
  type        = string
  sensitive   = true
  description = "Telegram bot token from @BotFather"
}

variable "telegram_webhook_secret" {
  type        = string
  sensitive   = true
  description = "Secret token registered with setWebhook"
}
