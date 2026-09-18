locals {
  ec2_ami = coalesce(var.ec2_ami_id, data.aws_ssm_parameter.al2023_ami.value)
}

# Fixed instances, no Auto Scaling Group. Host configuration belongs to Ansible.
resource "aws_instance" "moodle_a" {
  ami                         = local.ec2_ami
  instance_type               = var.moodle_instance_type
  subnet_id                   = aws_subnet.app_a.id
  vpc_security_group_ids      = [aws_security_group.moodle_sg.id]
  associate_public_ip_address = false
  key_name                    = aws_key_pair.deployer.key_name

  root_block_device {
    volume_size = var.moodle_root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tags = merge(local.moodle_tags, { Name = "moodle-app-a", Role = "moodle", Cron = "enabled" })
}

resource "aws_instance" "moodle_b" {
  ami                         = local.ec2_ami
  instance_type               = var.moodle_instance_type
  subnet_id                   = aws_subnet.app_b.id
  vpc_security_group_ids      = [aws_security_group.moodle_sg.id]
  associate_public_ip_address = false
  key_name                    = aws_key_pair.deployer.key_name

  root_block_device {
    volume_size = var.moodle_root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tags = merge(local.moodle_tags, { Name = "moodle-app-b", Role = "moodle", Cron = "disabled" })
}
