#--------------------------------------------------------------------------------------------------------
# EDGE MODULE
#--------------------------------------------------------------------------------------------------------
#
# Public ingress: ALB + target group + listener + target attachment + alb_sg.
# ALB lives in public subnets across two AZs (multi-AZ by construction).
# Listener is HTTP-only today; ACM cert + 443 listener + 80->443 redirect is
# Day 12 work, paired with Route 53 alias for stable external DNS.
#
# ALB DNS is not stable across recreation - every new ALB gets a fresh
# AWS-generated DNS string. Internal callers via terraform output read it
# dynamically; external callers should ride a Route 53 alias record instead.



#--------------------------------------------------------------------------------------------------------
# SECURITY GROUP
#--------------------------------------------------------------------------------------------------------
#
# alb_sg owns its identity + the public-internet ingress rule (HTTP/80).
# Egress to ec2_sg lives cross-module at root.



resource "aws_security_group" "alb_sg" {
  name        = "${var.project_name}-alb-sg"
  description = "ALB front door - accepts HTTP from the internet"
  vpc_id      = var.vpc_id

  tags = {
    Name        = "${var.project_name}-alb-sg"
    Environment = var.environment
  }
}


resource "aws_vpc_security_group_ingress_rule" "alb_http_from_internet" {
  security_group_id = aws_security_group.alb_sg.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "HTTP from internet"
}

resource "aws_vpc_security_group_ingress_rule" "alb_ingress_https" {
  security_group_id=aws_security_group.alb_sg.id
  cidr_ipv4="0.0.0.0/0"
  from_port=443
  to_port=443
  ip_protocol="tcp"
  description="HTTPS from internet"
}



#--------------------------------------------------------------------------------------------------------
# APPLICATION LOAD BALANCER
#--------------------------------------------------------------------------------------------------------
#
# L7 (HTTP-aware) load balancer. NLB would be the L4 alternative for raw TCP
# / static IP / source-IP-preservation workloads. Internal=false places it
# in public subnets and accepts internet traffic. enable_deletion_protection
# off so terraform destroy works cleanly in this learning env.



resource "aws_lb" "app" {
  name                       = "${var.project_name}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb_sg.id]
  subnets                    = var.public_subnet_ids
  enable_deletion_protection = false

  tags = {
    Name        = "${var.project_name}-alb"
    Environment = var.environment
  }
}

#--------------------------------------------------------------------------------------------------------
# Route 53
#--------------------------------------------------------------------------------------------------------

resource "aws_route53_record" "app" {
  count = var.create_https ? 1:0
  zone_id = var.zone_id
  name=var.domain_name
  type = "A"

  alias {
    name = aws_lb.app.dns_name
    zone_id = aws_lb.app.zone_id
    evaluate_target_health = true
  }
}

#--------------------------------------------------------------------------------------------------------
# ACM Certificate - 3 resources
#--------------------------------------------------------------------------------------------------------

# ACM Certificate — free TLS cert, auto-renews
resource "aws_acm_certificate" "app" {
  count = var.create_https ? 1:0
  domain_name = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# DNS validation CNAME record
resource "aws_route53_record" "cert_validation" {
  for_each = var.create_https ? {
    for dvo in aws_acm_certificate.app[0].domain_validation_options : dvo.domain_name => {
      name=dvo.resource_record_name
      record=dvo.resource_record_value
      type=dvo.resource_record_type
    }
  }:{}

  allow_overwrite = true
  zone_id = var.zone_id
  name = each.value.name
  type = each.value.type
  records = [each.value.record]
  ttl = 60
}

# Certificate validation waiter
resource "aws_acm_certificate_validation" "app" {
  count = var.create_https ? 1:0
  certificate_arn = aws_acm_certificate.app[0].arn

  validation_record_fqdns = [
    for record in aws_route53_record.cert_validation : record.fqdn
  ]
}

#--------------------------------------------------------------------------------------------------------
# TARGET GROUP + ATTACHMENT
#--------------------------------------------------------------------------------------------------------
#
# Health check on /health (DB-free liveness path), not on / or /ready. Test
# the process, not its dependencies - wiring the check at /ready would drain
# every target on a DB blip and convert a transient stall into a 502 outage.
#
# target_type = "instance" today; flips to "ip" when EKS arrives in Phase 3
# for pod-native targeting. Attachments don't support tags (AWS API
# limitation).



resource "aws_lb_target_group" "app" {
  name        = "${var.project_name}-alb-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Name        = "${var.project_name}-alb-tg"
    Environment = var.environment
  }
}


resource "aws_lb_target_group_attachment" "app" {
  target_group_arn = aws_lb_target_group.app.arn
  target_id        = var.instance_id
  port             = 8000
}



#--------------------------------------------------------------------------------------------------------
# LISTENER
#--------------------------------------------------------------------------------------------------------



resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = var.create_https ? "redirect":"forward"
    target_group_arn = var.create_https ? null : aws_lb_target_group.app.arn
    
    dynamic "redirect" {
      for_each = var.create_https ? [1]:[]
      content {
        port = "443"
        protocol = "HTTPS"
        status_code = "HTTP_301"
      } 
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.create_https ? 1:0
  load_balancer_arn = aws_lb.app.arn
  port=443
  protocol = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.app[0].certificate_arn
  default_action {
    type = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }

  tags = {
    Name="${var.project_name}-alb-listener-https"
    Environment=var.environment
  }
}