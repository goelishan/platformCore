# CloudWatch log group for the app's container logs.

resource "aws_cloudwatch_log_group" "app" {
    name="/platformcore/app"
    retention_in_days=7

    tags={
        Name="${var.project_name}-app-logs"
        Environment=var.environment
    }

}