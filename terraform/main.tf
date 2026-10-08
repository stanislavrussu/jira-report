terraform {
  # 1.11 is the floor for write-only arguments, used to keep the Jira token out of state.
  required_version = ">= 1.11, < 2.0"

  # Majors verified against this config; .terraform.lock.hcl pins the exact versions.
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" }
  }
}

provider "aws" {
  region = var.aws_region

  # null means "not set", which hands credential discovery back to the default chain.
  profile = var.aws_profile != "" ? var.aws_profile : null
}

# --- Jira credentials ---

# recovery_window_in_days = 0 so destroy frees the name immediately; the default 30-day window
# would block re-applying this stack under the same secret name.
resource "aws_secretsmanager_secret" "jira" {
  name                    = "${var.name_prefix}/credentials"
  description             = "Jira Cloud API credentials for the report Lambda"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "jira" {
  secret_id = aws_secretsmanager_secret.jira.id

  # Write-only: the value reaches AWS but is never persisted in terraform.tfstate.
  # Bump the version number to push a rotated token.
  secret_string_wo = jsonencode({
    jira_email     = var.jira_email
    jira_api_token = var.jira_api_token
  })
  secret_string_wo_version = 1
}

# --- SES identities ---

# Applying this sends a verification email to each address; both must be confirmed before sending.
resource "aws_sesv2_email_identity" "sender" {
  email_identity = var.report_sender
}

resource "aws_sesv2_email_identity" "recipient" {
  email_identity = var.report_recipient
}

# --- Lambda ---

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lambda_permissions" {
  statement {
    sid       = "ReadJiraCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.jira.arn]
  }

  statement {
    sid       = "SendReportEmailOnly"
    actions   = ["ses:SendEmail"]
    resources = [aws_sesv2_email_identity.sender.arn]

    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [var.report_sender]
    }
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name_prefix}-exec"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

# Lambda would otherwise create this itself, with no expiry.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

# No dependencies, so the deployment package is one source file.
data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/../jira_report.py"
  output_path = "${path.module}/lambda_package.zip"
}

resource "aws_lambda_function" "report" {
  function_name = var.name_prefix
  role          = aws_iam_role.lambda.arn
  runtime       = var.lambda_runtime
  handler       = "jira_report.lambda_handler"

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  # The default 3 s would cut a run off mid-request. This leaves room for a few retried Jira
  # calls, so the script reports the failure itself instead of being killed by the platform.
  # Memory covers the boto3 import; check Max Memory Used in the logs before lowering it.
  memory_size = 256
  timeout     = 300

  environment {
    variables = {
      JIRA_BASE_URL      = var.jira_base_url
      JIRA_PROJECT_KEY   = var.jira_project_key
      JIRA_SECRET_ID     = aws_secretsmanager_secret.jira.name
      JIRA_ISSUE_TYPES   = var.jira_issue_types
      JIRA_PRIORITIES    = var.jira_priorities
      REPORT_TIMEZONE    = var.report_timezone
      REPORT_WINDOW_DAYS = tostring(var.report_window_days)
      REPORT_SENDER      = var.report_sender
      REPORT_RECIPIENT   = var.report_recipient
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
    aws_secretsmanager_secret_version.jira,
  ]
}

# --- Schedule ---

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    # Account only. Adding aws:SourceArn breaks creation: CreateSchedule test-assumes the role
    # and sends an ARN that is not the schedule's own, so the condition can never match.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_iam_policy_document" "scheduler_permissions" {
  statement {
    sid       = "InvokeReportFunction"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.report.arn]
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.name_prefix}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_permissions.json
}

resource "aws_scheduler_schedule" "report" {
  name                         = var.name_prefix
  schedule_expression          = var.schedule_cron
  schedule_expression_timezone = var.report_timezone

  # A flexible window would fire somewhere around 10:00 rather than at 10:00.
  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.report.arn
    role_arn = aws_iam_role.scheduler.arn

    # AWS defaults to 185 attempts over 24 h; a report delivered the next day would be misleading.
    # Attempts is the binding limit here: backoff spreads 3 tries over minutes, not the full hour.
    retry_policy {
      maximum_retry_attempts       = 3
      maximum_event_age_in_seconds = 3600
    }
  }
}

# --- Spend guardrail ---

# Covers the whole account, not just this stack, so it also catches anything else left running.
# Notification-only budgets are free; only action-enabled ones are billed.
resource "aws_budgets_budget" "account" {
  name         = "${var.name_prefix}-account-spend"
  budget_type  = "COST"
  limit_amount = var.budget_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Forecast warns while there is still time to act; actual confirms it happened.
  # SES made report_sender confirm itself by email, so it is known to be a mailbox someone reads.
  dynamic "notification" {
    for_each = ["FORECASTED", "ACTUAL"]
    content {
      notification_type          = notification.value
      comparison_operator        = "GREATER_THAN"
      threshold                  = 100
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = [var.report_sender]
    }
  }
}
