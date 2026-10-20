# Legacy groups remain attached to existing EC2 instances during migration.
# Keep inline rules on these groups; new Moodle groups use standalone rules only.
resource "aws_security_group" "monitor_sg" {
  name        = "${var.project_name}-${var.environment}-monitor-sg"
  description = "Security group for monitoring and AI node"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from my IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = distinct(concat(
      [var.my_ip_cidr],
      var.ci_cd_ssh_cidr_blocks,
      var.ai_engineer_monitor_ssh_cidr_blocks,
    ))
  }

  ingress {
    description = "Grafana from administrator CIDR"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  ingress {
    description = "ICMP inside VPC"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-monitor-sg"
  })
}

