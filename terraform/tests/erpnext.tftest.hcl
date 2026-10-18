mock_provider "aws" {
  source = "./tests/mocks"
  mock_data "aws_availability_zones" {
    defaults = { names = ["ap-southeast-1a", "ap-southeast-1b"] }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
}

variables {
  moodle_allow_http     = true
  my_ip_cidr            = "192.0.2.10/32"
  ci_cd_ssh_cidr_blocks = []
  public_key_path       = "tests/fixtures/mock-public-key.txt"
  private_key_path      = "/unused/mock-private-key"
}

run "erpnext_disabled_by_default" {
  command = apply

  assert {
    condition     = length(aws_instance.erpnext) == 0 && length(aws_security_group.erpnext) == 0
    error_message = "ERPNext resources must remain absent unless explicitly enabled."
  }
  assert {
    condition     = output.erpnext_private_ip == "" && strcontains(output.erpnext_ansible_inventory, "Enable with -var='enable_erpnext=true'")
    error_message = "Disabled ERPNext must not expose a host or a misleading inventory."
  }
}

run "erpnext_host_is_private_and_isolated" {
  command = apply
  variables {
    enable_erpnext = true
  }

  assert {
    condition     = length(aws_instance.erpnext) == 1 && aws_instance.erpnext[0].subnet_id == aws_subnet.app_b.id && !aws_instance.erpnext[0].associate_public_ip_address
    error_message = "ERPNext must use a dedicated private EC2 in the app-B subnet with no public IP."
  }
  assert {
    condition     = aws_instance.erpnext[0].vpc_security_group_ids == toset([aws_security_group.erpnext[0].id]) && aws_instance.erpnext[0].root_block_device[0].encrypted
    error_message = "ERPNext must have its own security group and encrypted root storage."
  }
  assert {
    condition     = alltrue([for rule in [aws_vpc_security_group_ingress_rule.erpnext_monitor_ssh[0], aws_vpc_security_group_ingress_rule.erpnext_monitor_http[0], aws_vpc_security_group_ingress_rule.erpnext_monitor_node_exporter[0]] : rule.referenced_security_group_id == aws_security_group.monitor_sg.id])
    error_message = "ERPNext ingress must be restricted to the monitoring security group."
  }
  assert {
    condition     = strcontains(output.erpnext_ansible_inventory, "[erpnext]") && strcontains(output.erpnext_ansible_inventory, "erpnext-staging-01") && strcontains(output.erpnext_ansible_inventory, "ProxyJump")
    error_message = "Enabled ERPNext must produce a dedicated, monitor-jump inventory group."
  }
}
