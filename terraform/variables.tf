variable "aws_region" {
  description = "Region for every resource in this stack."
  type        = string
  default     = "eu-central-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile to deploy with. Leave empty to use the default credential chain, which is what CI and assumed roles need."
  type        = string
  default     = ""
}

variable "name_prefix" {
  description = "Prefix for all resource names. Encodes the cadence of this deployment."
  type        = string
  default     = "jira-report-weekly"
}

variable "jira_base_url" {
  description = "Jira Cloud site URL, for example https://acme.atlassian.net"
  type        = string

  validation {
    condition     = startswith(var.jira_base_url, "https://")
    error_message = "jira_base_url must start with https://."
  }
}

variable "jira_project_key" {
  description = "Key of the project to report on, for example ACME."
  type        = string

  validation {
    condition     = can(regex("^[A-Z][A-Z0-9]{1,9}$", var.jira_project_key))
    error_message = "jira_project_key must be 2-10 characters: uppercase letters and digits, starting with a letter."
  }
}

variable "jira_email" {
  description = "Atlassian account email used for API authentication."
  type        = string
}

variable "jira_api_token" {
  description = "Jira API token. Write-only, so it never reaches Terraform state. Prefer TF_VAR_jira_api_token over a file."
  type        = string
  sensitive   = true

  validation {
    # An empty token applies cleanly and only fails later, as a 401 inside the Lambda.
    condition     = length(var.jira_api_token) > 0
    error_message = "jira_api_token is empty. Paste the token, or remove the line and export TF_VAR_jira_api_token."
  }
}

variable "report_sender" {
  description = "From address. Verified in SES by this stack."
  type        = string
}

variable "report_recipient" {
  description = "Project lead address. Verified in SES by this stack because SES runs in sandbox mode."
  type        = string
}

variable "report_timezone" {
  description = "IANA time zone for the schedule and the reporting window. Must match the Jira account's time zone."
  type        = string
  default     = "Europe/Berlin"
}

variable "report_window_days" {
  description = "Length of the reporting window in days."
  type        = number
  default     = 7
}

variable "jira_issue_types" {
  description = "Comma-separated issue types to count, for example Bug,Task. Empty counts every type."
  type        = string
  default     = ""
}

variable "jira_priorities" {
  description = "Comma-separated priorities to count, for example High,Highest. Empty counts every priority."
  type        = string
  default     = ""
}

variable "schedule_cron" {
  description = "EventBridge Scheduler expression, evaluated in report_timezone."
  type        = string
  default     = "cron(0 10 ? * MON *)"
}

variable "lambda_runtime" {
  description = "Python runtime. The script works on 3.9 and newer. 3.14 is what the Lambda console marks Latest supported; 3.15 is listed but still Preview."
  type        = string
  default     = "python3.14"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention. Without an explicit value Lambda keeps logs forever."
  type        = number
  default     = 30
}

variable "budget_limit_usd" {
  description = "Monthly account spend above which AWS warns report_sender. The expected bill is well under a dollar, so a dollar means something unexpected is running."
  type        = number
  default     = 1
}
