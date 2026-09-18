resource "aws_lb" "moodle" {
  name                       = "${local.moodle_prefix}-alb"
  load_balancer_type         = "application"
  internal                   = false
  ip_address_type            = "ipv4"
  subnets                    = [aws_subnet.public_a.id, aws_subnet.public_b.id]
  security_groups            = [aws_security_group.alb_sg.id]
  drop_invalid_header_fields = true
  idle_timeout               = 60
  tags                       = local.moodle_tags

  lifecycle {
    precondition {
      condition     = var.moodle_certificate_arn != null || var.moodle_allow_http
      error_message = "Provide an ACM certificate, or explicitly enable moodle_allow_http for staging smoke tests."
    }
    precondition {
      condition     = var.moodle_certificate_arn == null ? true : var.moodle_hostname != null
      error_message = "HTTPS requires a DNS hostname covered by the certificate; the ALB DNS name is not your certificate hostname."
    }
    precondition {
      condition     = var.moodle_certificate_arn == null ? true : try(split(":", var.moodle_certificate_arn)[3] == var.aws_region, false)
      error_message = "The ACM certificate must be in the same AWS region as the ALB."
    }
  }
}

resource "aws_lb_target_group" "moodle" {
  name                 = "${local.moodle_prefix}-tg"
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "instance"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    enabled             = true
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  stickiness {
    type    = "lb_cookie"
    enabled = false
  }

  tags = local.moodle_tags
}

resource "aws_lb_target_group_attachment" "moodle_a" {
  target_group_arn = aws_lb_target_group.moodle.arn
  target_id        = aws_instance.moodle_a.id
  port             = 8080
}

resource "aws_lb_target_group_attachment" "moodle_b" {
  target_group_arn = aws_lb_target_group.moodle.arn
  target_id        = aws_instance.moodle_b.id
  port             = 8080
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.moodle.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.moodle_certificate_arn == null ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.moodle.arn
    }
  }
  dynamic "default_action" {
    for_each = var.moodle_certificate_arn != null ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.moodle_certificate_arn == null ? 0 : 1
  load_balancer_arn = aws_lb.moodle.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.moodle_certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.moodle.arn
  }
}
