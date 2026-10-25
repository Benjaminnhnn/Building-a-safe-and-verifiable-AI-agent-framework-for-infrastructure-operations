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
  iam_instance_profile        = aws_iam_instance_profile.monitor_audit.name

  root_block_device {
    volume_size = coalesce(var.monitor_root_volume_size, var.root_volume_size)
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint          = "enabled"
    http_tokens            = "required"
    instance_metadata_tags = "enabled"
  }

  # The SEC-01 audit reads these through IMDSv2. EC2 tag values cannot hold
  # commas, so reviewed source groups are colon-separated.
  tags = merge(local.common_tags, {
    Name                         = "monitor-ai-01"
    Role                         = "monitoring"
    AiopsRdsSecurityGroupId      = aws_security_group.rds_sg.id
    AiopsRdsReviewedSourceGroups = join(":", [aws_security_group.moodle_sg.id, aws_security_group.monitor_sg.id])
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

