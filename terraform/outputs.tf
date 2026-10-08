output "function_name" {
  description = "Invoke manually with: aws lambda invoke --function-name <this> --payload '{}' /dev/stdout"
  value       = aws_lambda_function.report.function_name
}

output "log_group" {
  description = "Tail with: aws logs tail <this> --follow"
  value       = aws_cloudwatch_log_group.lambda.name
}

output "schedule" {
  description = "Schedule expression and the time zone it is evaluated in."
  value       = "${var.schedule_cron} (${var.report_timezone})"
}

output "ses_identities_to_confirm" {
  description = "Both addresses must be confirmed from their inbox before the first send succeeds."
  value       = [var.report_sender, var.report_recipient]
}
