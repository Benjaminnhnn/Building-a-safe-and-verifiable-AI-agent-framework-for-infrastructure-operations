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
    cidr_blocks = distinct(concat([var.my_ip_cidr], var.ci_cd_ssh_cidr_blocks))
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

resource "aws_security_group" "web_sg" {
  name        = "${var.project_name}-${var.environment}-web-sg"
  description = "Security group for web node"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from my IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = distinct(concat([var.my_ip_cidr], var.ci_cd_ssh_cidr_blocks))
  }

  ingress {
    description = "HTTP from administrator CIDR"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  ingress {
    description = "HTTPS from administrator CIDR"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  ingress {
    description = "Staging frontend from administrator CIDR"
    from_port   = 18081
    to_port     = 18081
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  ingress {
    description     = "Nginx metrics proxy from monitor node"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "Node Exporter from monitor node"
    from_port       = 9100
    to_port         = 9100
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "cAdvisor from monitor node"
    from_port       = 8088
    to_port         = 8088
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "Internal health check from monitor node"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
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
    Name = "${var.project_name}-${var.environment}-web-sg"
  })
}

resource "aws_security_group" "core_sg" {
  name        = "${var.project_name}-${var.environment}-core-sg"
  description = "Security group for core/app node"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from my IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = distinct(concat([var.my_ip_cidr], var.ci_cd_ssh_cidr_blocks))
  }

  ingress {
    description     = "API from web and monitor nodes only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.web_sg.id, aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "API or app access from web node"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.web_sg.id]
  }

  ingress {
    description     = "Staging API access from web node"
    from_port       = 18080
    to_port         = 18080
    protocol        = "tcp"
    security_groups = [aws_security_group.web_sg.id]
  }

  ingress {
    description     = "API or app access from monitor node"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "Staging API access from monitor node"
    from_port       = 18080
    to_port         = 18080
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "Node Exporter from monitor node"
    from_port       = 9100
    to_port         = 9100
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "Postgres Exporter from monitor node"
    from_port       = 9187
    to_port         = 9187
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "Staging Postgres Exporter from monitor node"
    from_port       = 19187
    to_port         = 19187
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
  }

  ingress {
    description     = "cAdvisor from monitor node"
    from_port       = 8088
    to_port         = 8088
    protocol        = "tcp"
    security_groups = [aws_security_group.monitor_sg.id]
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
    Name = "${var.project_name}-${var.environment}-core-sg"
  })
}
