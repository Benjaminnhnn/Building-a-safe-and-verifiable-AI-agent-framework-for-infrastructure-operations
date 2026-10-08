locals {
  tags = {
    Project     = "moodle-aiops-local"
    Environment = "local"
    ManagedBy   = "Terraform"
  }
}

resource "aws_vpc" "local" {
  cidr_block           = "10.77.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "moodle-aiops-local-vpc" })
}

resource "aws_subnet" "workloads" {
  vpc_id                  = aws_vpc.local.id
  cidr_block              = "10.77.1.0/24"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = false
  tags                    = merge(local.tags, { Name = "moodle-aiops-local-workloads" })
}

resource "aws_subnet" "workloads_b" {
  vpc_id                  = aws_vpc.local.id
  cidr_block              = "10.77.2.0/24"
  availability_zone       = "us-east-1b"
  map_public_ip_on_launch = false
  tags                    = merge(local.tags, { Name = "moodle-aiops-local-workloads-b" })
}

resource "aws_security_group" "workloads" {
  name        = "moodle-aiops-local-workloads"
  description = "Metadata-only local lab security group"
  vpc_id      = aws_vpc.local.id
  tags        = local.tags
}

resource "aws_iam_role" "observer" {
  name = "moodle-aiops-local-observer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_instance_profile" "observer" {
  name = "moodle-aiops-local-observer"
  role = aws_iam_role.observer.name
  tags = local.tags
}

# Floci maps this EC2 resource to a real local Docker container. It is a control
# plane/runtime compatibility probe, not the host for Moodle or the AI agent.
resource "aws_instance" "control_plane_probe" {
  ami                    = "ami-ubuntu2404-amd64"
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.workloads.id
  vpc_security_group_ids = [aws_security_group.workloads.id]
  iam_instance_profile   = aws_iam_instance_profile.observer.name

  tags = merge(local.tags, { Name = "moodle-aiops-local-ec2-probe" })
}

resource "aws_db_subnet_group" "moodle" {
  name       = "moodle-aiops-local-db"
  subnet_ids = [aws_subnet.workloads.id, aws_subnet.workloads_b.id]
  tags       = local.tags
}

resource "aws_db_instance" "moodle" {
  identifier        = "moodle-aiops-local-postgres"
  engine            = "postgres"
  engine_version    = "16.4"
  instance_class    = "db.t3.micro"
  allocated_storage = 20
  db_name           = "moodle"
  username          = "moodle_local"
  password          = var.moodle_db_password
  # Floci exposes its Postgres relay through this host-published port.
  port                   = 7001
  db_subnet_group_name   = aws_db_subnet_group.moodle.name
  vpc_security_group_ids = [aws_security_group.workloads.id]
  publicly_accessible    = false
  skip_final_snapshot    = true
  deletion_protection    = false
  apply_immediately      = true
  tags                   = local.tags
}

resource "aws_efs_file_system" "moodledata_metadata" {
  creation_token = "moodle-aiops-local-data"
  encrypted      = true
  tags           = local.tags
}

resource "aws_lb" "moodle" {
  name                 = "moodle-aiops-local-alb"
  internal             = false
  load_balancer_type   = "application"
  preserve_host_header = true
  subnets              = [aws_subnet.workloads.id, aws_subnet.workloads_b.id]
  tags                 = local.tags
}

resource "aws_lb_target_group" "moodle" {
  name        = "moodle-aiops-local-web"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.local.id

  health_check {
    enabled             = true
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = "/healthz.php"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = local.tags
}

resource "aws_lb_listener" "moodle_http" {
  load_balancer_arn = aws_lb.moodle.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.moodle.arn
  }
}

resource "aws_lb_target_group_attachment" "moodle" {
  for_each         = var.moodle_target_ips
  target_group_arn = aws_lb_target_group.moodle.arn
  target_id        = each.value
  port             = 8080
}
