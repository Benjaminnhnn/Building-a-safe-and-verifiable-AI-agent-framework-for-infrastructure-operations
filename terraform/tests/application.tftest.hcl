mock_provider "aws" {
  source = "./tests/mocks"
  mock_data "aws_availability_zones" {
    defaults = { names = ["ap-southeast-1a", "ap-southeast-1b"] }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
}

override_resource {
  target = aws_instance.moodle_a
  values = { private_ip = "10.10.11.10" }
}
override_resource {
  target = aws_instance.moodle_b
  values = { private_ip = "10.10.12.10" }
}
override_resource {
  target = aws_eip.monitor
  values = { public_ip = "192.0.2.20" }
}

variables {
  my_ip_cidr                    = "192.0.2.10/32"
  ci_cd_ssh_cidr_blocks         = []
  public_key_path               = "tests/fixtures/mock-public-key.txt"
  private_key_path              = "/unused/mock-private-key"
  moodle_allow_http             = true
  enable_legacy_demo            = false
  monitor_use_management_subnet = true
  monitor_root_volume_size      = 30
}

run "fresh_moodle_infrastructure" {
  command = apply

  assert {
    condition     = yamldecode(output.moodle_ansible_inventory).all.vars.ansible_host_key_checking && yamldecode(output.moodle_ansible_inventory).all.vars.ansible_ssh_host_key_checking
    error_message = "The generated inventory must enable host-key checking even with legacy Ansible defaults."
  }

  assert {
    condition     = length(aws_instance.web) == 0 && length(aws_instance.core) == 0 && length(aws_eip.web) == 0 && length(aws_eip.core) == 0 && aws_instance.monitor.subnet_id == aws_subnet.management.id
    error_message = "Fresh deployment must omit legacy EC2/EIPs and place monitor in management."
  }
  assert {
    condition     = aws_instance.moodle_a.subnet_id == aws_subnet.app_a.id && aws_instance.moodle_b.subnet_id == aws_subnet.app_b.id && !aws_instance.moodle_a.associate_public_ip_address && !aws_instance.moodle_b.associate_public_ip_address && aws_instance.moodle_a.vpc_security_group_ids == toset([aws_security_group.moodle_sg.id]) && aws_instance.moodle_b.vpc_security_group_ids == toset([aws_security_group.moodle_sg.id])
    error_message = "Fixed Moodle instances must use private app subnets and Moodle SG."
  }
  assert {
    condition     = aws_instance.moodle_a.ami == aws_instance.moodle_b.ami && alltrue([for host in [aws_instance.moodle_a, aws_instance.moodle_b] : one(host.root_block_device).encrypted && one(host.root_block_device).volume_size >= 30 && one(host.metadata_options).http_tokens == "required" && one(host.metadata_options).http_put_response_hop_limit == 1])
    error_message = "Moodle nodes must share an AMI, have encrypted release storage and require IMDSv2."
  }
  assert {
    condition     = !aws_lb.moodle.internal && aws_lb.moodle.load_balancer_type == "application" && aws_lb.moodle.subnets == toset([aws_subnet.public_a.id, aws_subnet.public_b.id]) && aws_lb.moodle.security_groups == toset([aws_security_group.alb_sg.id])
    error_message = "Internet-facing ALB must span both public subnets and use ALB SG."
  }
  assert {
    condition     = aws_lb_target_group.moodle.target_type == "instance" && aws_lb_target_group.moodle.port == 8080 && aws_lb_target_group_attachment.moodle_a.target_id == aws_instance.moodle_a.id && aws_lb_target_group_attachment.moodle_b.target_id == aws_instance.moodle_b.id && aws_lb_target_group_attachment.moodle_a.target_group_arn == aws_lb_target_group.moodle.arn && aws_lb_target_group_attachment.moodle_b.target_group_arn == aws_lb_target_group.moodle.arn && one(aws_lb_target_group.moodle.health_check).path == "/healthz" && one(aws_lb_target_group.moodle.health_check).matcher == "200" && !one(aws_lb_target_group.moodle.stickiness).enabled
    error_message = "ALB must register both fixed targets, check readiness and avoid sticky-session masking."
  }
  assert {
    condition     = length(aws_lb_listener.https) == 0 && one(aws_lb_listener.http.default_action).type == "forward"
    error_message = "Explicit HTTP smoke-test mode must forward to Moodle without an HTTPS listener."
  }
  assert {
    condition     = yamldecode(output.moodle_ansible_inventory).all.children.moodle.hosts["moodle-app-a"].ansible_host == "10.10.11.10" && yamldecode(output.moodle_ansible_inventory).all.children.moodle.hosts["moodle-app-a"].moodle_cron_enabled && !yamldecode(output.moodle_ansible_inventory).all.children.moodle.hosts["moodle-app-b"].moodle_cron_enabled && strcontains(output.moodle_ssh_config, "ProxyJump monitor-ai-01") && strcontains(output.moodle_ssh_config, "ForwardAgent no") && strcontains(output.moodle_ssh_config, "StrictHostKeyChecking yes")
    error_message = "Inventory must expose both private nodes, one cron owner and secure bastion access."
  }
}

run "https_redirect" {
  command = plan
  variables {
    moodle_allow_http      = false
    moodle_certificate_arn = "arn:aws:acm:ap-southeast-1:000000000000:certificate/00000000-0000-0000-0000-000000000000"
    moodle_hostname        = "moodle.example.org"
  }
  assert {
    condition     = length(aws_lb_listener.https) == 1 && one(aws_lb_listener.http.default_action).type == "redirect" && one(one(aws_lb_listener.http.default_action).redirect).protocol == "HTTPS" && aws_lb_listener.https[0].protocol == "HTTPS" && output.moodle_application.url == "https://moodle.example.org"
    error_message = "An ACM certificate must enable HTTPS and redirect HTTP."
  }
}

run "reject_implicit_plaintext" {
  command = plan
  variables { moodle_allow_http = false }
  expect_failures = [aws_lb.moodle]
}

run "pinned_ami" {
  command = plan
  variables { ec2_ami_id = "ami-abcdef01234567890" }
  assert {
    condition     = aws_instance.moodle_a.ami == "ami-abcdef01234567890" && aws_instance.moodle_b.ami == "ami-abcdef01234567890" && aws_instance.monitor.ami == "ami-abcdef01234567890"
    error_message = "AMI pin must apply to both Moodle nodes and monitor."
  }
}

run "reject_https_without_hostname" {
  command = plan
  variables {
    moodle_certificate_arn = "arn:aws:acm:ap-southeast-1:000000000000:certificate/00000000-0000-0000-0000-000000000000"
  }
  expect_failures = [aws_lb.moodle]
}

run "reject_cross_region_certificate" {
  command = plan
  variables {
    moodle_certificate_arn = "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000000"
    moodle_hostname        = "moodle.example.org"
  }
  expect_failures = [aws_lb.moodle]
}

run "reject_undersized_release_disk" {
  command = plan
  variables { moodle_root_volume_size = 12 }
  expect_failures = [var.moodle_root_volume_size]
}
