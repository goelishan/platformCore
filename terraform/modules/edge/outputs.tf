#--------------------------------------------------------------------------------------------------------
# EDGE MODULE OUTPUTS
#--------------------------------------------------------------------------------------------------------



output "alb_sg_id" {
  value = aws_security_group.alb_sg.id
}

output "alb_dns_name" {
  value = aws_lb.app.dns_name
}

output "alb_url" {
  value = "http://${aws_lb.app.dns_name}"
}

output "app_url" {
  description = "App URL — HTTPS via Route 53 if create_https = true, HTTP via ALB DNS otherwise"
  value       = var.create_https ? "https://${aws_route53_record.app[0].fqdn}" : "http://${aws_lb.app.dns_name}"
}

output "certificate_arn" {
  description = "ACM certificate ARN (empty string when create_https = false)"
  value       = var.create_https ? aws_acm_certificate_validation.app[0].certificate_arn : ""
}