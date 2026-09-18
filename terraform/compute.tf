resource "aws_key_pair" "deployer" {
  key_name   = "${var.project_name}-${var.environment}-key"
  public_key = file(pathexpand(var.public_key_path))

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-key"
  })
}

resource "aws_instance" "monitor" {
  ami                         = local.ec2_ami
  instance_type               = var.monitor_instance_type
  subnet_id                   = var.monitor_use_management_subnet ? aws_subnet.management.id : aws_subnet.public_a.id
  vpc_security_group_ids      = [aws_security_group.monitor_sg.id]
  associate_public_ip_address = true
  key_name                    = aws_key_pair.deployer.key_name

  root_block_device {
    volume_size = coalesce(var.monitor_root_volume_size, var.root_volume_size)
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = merge(local.common_tags, {
    Name = "monitor-ai-01"
    Role = "monitoring"
  })
}

resource "aws_instance" "web" {
  count                       = var.enable_legacy_demo ? 1 : 0
  ami                         = local.ec2_ami
  instance_type               = var.web_instance_type
  subnet_id                   = aws_subnet.public_a.id
  vpc_security_group_ids      = [aws_security_group.web_sg.id]
  associate_public_ip_address = true
  key_name                    = aws_key_pair.deployer.key_name

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = merge(local.common_tags, {
    Name = "bank-web-01"
    Role = "web"
  })
}

resource "aws_instance" "core" {
  count                       = var.enable_legacy_demo ? 1 : 0
  ami                         = local.ec2_ami
  instance_type               = var.core_instance_type
  subnet_id                   = aws_subnet.public_a.id
  vpc_security_group_ids      = [aws_security_group.core_sg.id]
  associate_public_ip_address = true
  key_name                    = aws_key_pair.deployer.key_name

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = merge(local.common_tags, {
    Name = "bank-core-01"
    Role = "core"
  })
}

# Allocate Elastic IPs for static public IPs
resource "aws_eip" "monitor" {
  instance = aws_instance.monitor.id
  domain   = "vpc"

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-monitor-eip"
  })

  depends_on = [aws_instance.monitor]
}

resource "aws_eip" "web" {
  count    = var.enable_legacy_demo ? 1 : 0
  instance = aws_instance.web[0].id
  domain   = "vpc"

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-web-eip"
  })

  depends_on = [aws_instance.web]
}

resource "aws_eip" "core" {
  count    = var.enable_legacy_demo ? 1 : 0
  instance = aws_instance.core[0].id
  domain   = "vpc"

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-core-eip"
  })

  depends_on = [aws_instance.core]
}
