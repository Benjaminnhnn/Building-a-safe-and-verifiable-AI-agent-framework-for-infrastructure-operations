# Requires Terraform >= 1.7. All resources use a mock provider; no AWS calls.
mock_provider "aws" {
  source = "./tests/mocks"
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-southeast-1a", "ap-southeast-1b"]
    }
  }
  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }
}

variables {
  moodle_allow_http     = true
  my_ip_cidr            = "192.0.2.10/32"
  ci_cd_ssh_cidr_blocks = ["198.51.100.20/32"]
  public_key_path       = "tests/fixtures/mock-public-key.txt"
  private_key_path      = "/unused/mock-private-key"
}

run "network_and_security_contract" {
  command = apply

  assert {
    condition     = toset([aws_subnet.public_a.cidr_block, aws_subnet.public_b.cidr_block, aws_subnet.app_a.cidr_block, aws_subnet.app_b.cidr_block, aws_subnet.data_a.cidr_block, aws_subnet.data_b.cidr_block, aws_subnet.management.cidr_block]) == toset(["10.10.1.0/24", "10.10.2.0/24", "10.10.11.0/24", "10.10.12.0/24", "10.10.21.0/24", "10.10.22.0/24", "10.10.31.0/24"])
    error_message = "The seven subnet CIDRs must match the infrastructure design without overlap."
  }

  assert {
    condition     = aws_route_table_association.public_b.route_table_id == aws_route_table.public_rt.id && aws_route_table_association.management.route_table_id == aws_route_table.public_rt.id && aws_route.default_internet_route.gateway_id == aws_internet_gateway.igw.id
    error_message = "ALB/NAT public subnets and the prepared management subnet must have the IGW route."
  }

  assert {
    condition     = aws_subnet.app_a.availability_zone != aws_subnet.app_b.availability_zone && aws_subnet.public_a.availability_zone == aws_subnet.app_a.availability_zone && aws_subnet.public_b.availability_zone == aws_subnet.app_b.availability_zone && aws_subnet.data_a.availability_zone == aws_subnet.app_a.availability_zone && aws_subnet.data_b.availability_zone == aws_subnet.app_b.availability_zone
    error_message = "Public/app/data tiers must align within each AZ and span two distinct AZs."
  }
  assert {
    condition     = alltrue([for s in [aws_subnet.app_a, aws_subnet.app_b, aws_subnet.data_a, aws_subnet.data_b, aws_subnet.management] : !s.map_public_ip_on_launch])
    error_message = "Private subnets and management must not auto-assign public IPs."
  }
  assert {
    condition     = aws_route.app_a_internet.nat_gateway_id == aws_nat_gateway.a.id && aws_route.app_b_internet.nat_gateway_id == aws_nat_gateway.b.id && aws_nat_gateway.a.subnet_id == aws_subnet.public_a.id && aws_nat_gateway.b.subnet_id == aws_subnet.public_b.id && aws_route_table_association.app_a.route_table_id == aws_route.app_a_internet.route_table_id && aws_route_table_association.app_b.route_table_id == aws_route.app_b_internet.route_table_id
    error_message = "Each app subnet must route outbound via the NAT in its own AZ."
  }
  assert {
    condition     = length(aws_route_table.data_a.route) == 0 && length(aws_route_table.data_b.route) == 0 && aws_route_table_association.data_a.route_table_id == aws_route_table.data_a.id && aws_route_table_association.data_b.route_table_id == aws_route_table.data_b.id
    error_message = "Data subnets must use isolated route tables without an Internet/NAT default route."
  }
  assert {
    condition     = alltrue([for r in aws_security_group.monitor_sg.ingress : !contains(coalesce(r.cidr_blocks, []), "0.0.0.0/0") && !contains([9090, 9093, 8000, 18000, 19121], r.from_port)]) && alltrue([for r in concat(tolist(aws_security_group.web_sg.ingress), tolist(aws_security_group.core_sg.ingress)) : !contains(coalesce(r.cidr_blocks, []), "0.0.0.0/0")])
    error_message = "Legacy hosts must not expose app/admin/monitoring services to the entire Internet."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_to_moodle.referenced_security_group_id == aws_security_group.alb_sg.id && aws_vpc_security_group_ingress_rule.alb_to_moodle.from_port == 8080 && aws_vpc_security_group_egress_rule.alb_to_moodle.referenced_security_group_id == aws_security_group.moodle_sg.id
    error_message = "ALB must connect to the Moodle target port via matching SG ingress/egress."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.moodle_to_rds.referenced_security_group_id == aws_security_group.moodle_sg.id && aws_vpc_security_group_ingress_rule.monitor_to_rds.referenced_security_group_id == aws_security_group.monitor_sg.id && aws_vpc_security_group_ingress_rule.moodle_to_efs.referenced_security_group_id == aws_security_group.moodle_sg.id && aws_vpc_security_group_ingress_rule.moodle_to_rds.from_port == 5432 && aws_vpc_security_group_ingress_rule.moodle_to_efs.from_port == 2049
    error_message = "Only approved app/monitor SGs may connect to RDS PostgreSQL and only Moodle may connect to EFS."
  }
  assert {
    condition     = alltrue([for r in [aws_vpc_security_group_ingress_rule.monitor_ssh, aws_vpc_security_group_ingress_rule.monitor_node_exporter, aws_vpc_security_group_ingress_rule.monitor_cadvisor] : r.referenced_security_group_id == aws_security_group.monitor_sg.id && r.security_group_id == aws_security_group.moodle_sg.id])
    error_message = "Moodle SSH and exporter ports must be reachable only from the monitor SG."
  }
  assert {
    condition     = aws_instance.monitor.subnet_id == aws_subnet.public_a.id && aws_instance.web[0].subnet_id == aws_subnet.public_a.id && aws_instance.core[0].subnet_id == aws_subnet.public_a.id
    error_message = "Network expansion must preserve legacy EC2 placement to avoid subnet-triggered replacement."
  }
}

run "reject_public_admin_access" {
  command = plan
  variables {
    my_ip_cidr = "0.0.0.0/0"
  }
  expect_failures = [var.my_ip_cidr]
}

run "reject_public_ci_access" {
  command = plan
  variables {
    ci_cd_ssh_cidr_blocks = ["0.0.0.0/0"]
  }
  expect_failures = [var.ci_cd_ssh_cidr_blocks]
}

run "reject_invalid_cidr" {
  command = plan
  variables {
    my_ip_cidr = "not-an-ip/32"
  }
  expect_failures = [var.my_ip_cidr]
}

run "reject_production_moodle" {
  command = plan
  variables {
    moodle_environment = "production"
  }
  expect_failures = [var.moodle_environment]
}
