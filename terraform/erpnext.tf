variable "enable_erpnext" {
  type        = bool
  default     = false
  description = "Create the isolated ERPNext thesis-validation EC2. Disabled by default to avoid cost."
}

variable "erpnext_instance_type" {
  type        = string
  default     = "t3.xlarge"
  description = "Instance type for the isolated ERPNext staging host. Review cost and capacity before apply."
}

variable "erpnext_root_volume_size" {
  type    = number
  default = 80
  validation {
    condition     = var.erpnext_root_volume_size >= 40 && floor(var.erpnext_root_volume_size) == var.erpnext_root_volume_size
    error_message = "ERPNext root volume must be a whole number of at least 40 GiB."
  }
}

resource "aws_security_group" "erpnext" {
  count       = var.enable_erpnext ? 1 : 0
  name        = "${local.moodle_prefix}-erpnext-sg"
  description = "Isolated ERPNext staging host; reachable only from monitoring inside the VPC."
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-erpnext-sg", Role = "erpnext-staging" })
}

resource "aws_vpc_security_group_ingress_rule" "erpnext_monitor_ssh" {
  count                        = var.enable_erpnext ? 1 : 0
  security_group_id            = aws_security_group.erpnext[0].id
  referenced_security_group_id = aws_security_group.monitor_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 22
  to_port                      = 22
  description                  = "Ansible/SSH from monitoring host only"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "erpnext_monitor_http" {
  count                        = var.enable_erpnext ? 1 : 0
  security_group_id            = aws_security_group.erpnext[0].id
  referenced_security_group_id = aws_security_group.monitor_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  description                  = "Private staging probes from monitoring host only"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "erpnext_monitor_node_exporter" {
  count                        = var.enable_erpnext ? 1 : 0
  security_group_id            = aws_security_group.erpnext[0].id
  referenced_security_group_id = aws_security_group.monitor_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 9100
  to_port                      = 9100
  description                  = "Node exporter from monitoring host only"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_egress_rule" "erpnext_https" {
  count             = var.enable_erpnext ? 1 : 0
  security_group_id = aws_security_group.erpnext[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Pull pinned container images and OS packages via private subnet NAT"
  tags              = local.moodle_tags
}

resource "aws_instance" "erpnext" {
  count                       = var.enable_erpnext ? 1 : 0
  ami                         = local.ec2_ami
  instance_type               = var.erpnext_instance_type
  subnet_id                   = aws_subnet.app_b.id
  vpc_security_group_ids      = [aws_security_group.erpnext[0].id]
  associate_public_ip_address = false
  key_name                    = aws_key_pair.deployer.key_name

  root_block_device {
    volume_size = var.erpnext_root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tags = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-erpnext-staging-01", Role = "erpnext-staging", IsolatedFrom = "moodle" })
}

output "erpnext_private_ip" {
  value       = try(aws_instance.erpnext[0].private_ip, "")
  description = "Private IP of the optional isolated ERPNext staging host; blank unless enable_erpnext=true."
}

output "erpnext_ansible_inventory" {
  value = var.enable_erpnext ? format(
    "[monitor]\nmonitor-ai-01 ansible_host=%s ansible_user=%s\n\n[erpnext]\nerpnext-staging-01 ansible_host=%s private_ip=%s ansible_user=%s ansible_ssh_common_args='-o ProxyJump=%s@%s -o IdentitiesOnly=yes'\n\n[all:vars]\nansible_python_interpreter=/usr/bin/python3\nansible_ssh_private_key_file=%s\n",
    aws_eip.monitor.public_ip,
    var.ssh_user,
    aws_instance.erpnext[0].private_ip,
    aws_instance.erpnext[0].private_ip,
    var.ssh_user,
    var.ssh_user,
    aws_eip.monitor.public_ip,
    abspath(pathexpand(var.private_key_path)),
  ) : "[erpnext]\n# Enable with -var='enable_erpnext=true' to generate this inventory.\n"
  description = "Dedicated inventory output for isolated ERPNext staging, separate from Moodle application hosts."
}
