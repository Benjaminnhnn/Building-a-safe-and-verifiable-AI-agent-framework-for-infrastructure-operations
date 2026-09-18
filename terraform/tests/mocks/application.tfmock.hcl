# Provider schema validates these ARNs even when AWS calls are mocked.
mock_resource "aws_lb" {
  defaults = {
    arn      = "arn:aws:elasticloadbalancing:ap-southeast-1:000000000000:loadbalancer/app/mock-alb/0123456789abcdef"
    dns_name = "mock-alb.example.invalid"
  }
}
mock_resource "aws_lb_target_group" {
  defaults = {
    arn = "arn:aws:elasticloadbalancing:ap-southeast-1:000000000000:targetgroup/mock-targets/0123456789abcdef"
  }
}
