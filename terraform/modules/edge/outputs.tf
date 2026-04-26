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