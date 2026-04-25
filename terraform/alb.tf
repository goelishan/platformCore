# Application Load Balancer - public entry point for the app tier.
#
# Four resources: the ALB itself, the target group (where the ALB forwards to),
# the attachment (binds the EC2 to the target group), and the listener (the
# port+protocol the ALB accepts traffic on).
#
# Design decisions worth explaining in review:
#
# - ALB (L7), not NLB. HTTP-aware routing, built-in health checks, and future
#   WAF / OIDC / path-routing all come from ALB. NLB would be right for TCP/UDP
#   or static-IP workloads, neither of which apply here.
# - internal=false, in public subnets. Accepts internet traffic.
# - subnets = aws_subnet.public[*].id - splat gives both public subnets, which
#   satisfies AWS's "ALB must span at least two AZs" requirement.
# - Port 80 only for now (no TLS cert yet). Production should terminate on 443
#   with an ACM cert and redirect 80 -> 443. Tracked as a Phase-2-late fix.
# - target_type = instance. When we move to EKS in Phase 3, this flips to "ip"
#   for pod-native targeting.
# - enable_deletion_protection = false for this phase so terraform destroy
#   works cleanly. Set to true for any real prod deployment.

resource "aws_lb" "app" {
  name                       = "${var.project_name}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb_sg.id]
  subnets                    = aws_subnet.public[*].id
  enable_deletion_protection = false

  tags = {
    Name        = "${var.project_name}-alb"
    Environment = var.environment
  }
}

resource "aws_lb_target_group" "app" {
  name        = "${var.project_name}-alb-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
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

# Attachment is pure glue - binds the EC2 instance to the target group.
# AWS does not support tags on this resource type.
resource "aws_lb_target_group_attachment" "app" {
  target_group_arn = aws_lb_target_group.app.arn
  target_id        = aws_instance.app.id
  port             = 8000
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }

  tags = {
    Name        = "${var.project_name}-alb-listener-http"
    Environment = var.environment
  }
}
