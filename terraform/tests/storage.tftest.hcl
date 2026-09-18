# All runs use a mocked AWS provider. "apply" here never creates cloud resources.
mock_provider "aws" {
  source = "./tests/mocks"
  mock_data "aws_availability_zones" {
    defaults = { names = ["ap-southeast-1a", "ap-southeast-1b"] }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_resource "aws_efs_file_system" {
    defaults = {
      # AWS leaves this empty for Regional EFS; avoid a random mock AZ.
      availability_zone_name = ""
      arn                    = "arn:aws:elasticfilesystem:ap-southeast-1:000000000000:file-system/fs-0123456789abcdef0"
    }
  }
  mock_resource "aws_efs_access_point" {
    defaults = {
      arn = "arn:aws:elasticfilesystem:ap-southeast-1:000000000000:access-point/fsap-0123456789abcdef0"
    }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address = "mock-postgres.example.ap-southeast-1.rds.amazonaws.com"
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:ap-southeast-1:000000000000:secret:mock-rds-secret"
        secret_status = "active"
        kms_key_id    = "mock-kms-key"
      }]
    }
  }
}

variables {
  moodle_allow_http     = true
  my_ip_cidr            = "192.0.2.10/32"
  ci_cd_ssh_cidr_blocks = []
  public_key_path       = "tests/fixtures/mock-public-key.txt"
  private_key_path      = "/unused/mock-private-key"
}

run "storage_contract" {
  command = apply

  assert {
    condition     = aws_db_instance.moodle.engine == "postgres" && aws_db_subnet_group.moodle.subnet_ids == toset([aws_subnet.data_a.id, aws_subnet.data_b.id]) && aws_db_instance.moodle.db_subnet_group_name == aws_db_subnet_group.moodle.name && aws_db_instance.moodle.vpc_security_group_ids == toset([aws_security_group.rds_sg.id])
    error_message = "RDS PostgreSQL must use the private two-AZ database subnet group and the database SG."
  }
  assert {
    condition     = aws_db_instance.moodle.storage_encrypted && aws_db_instance.moodle.backup_retention_period >= 1 && !aws_db_instance.moodle.deletion_protection && aws_db_instance.moodle.skip_final_snapshot && aws_db_instance.moodle.delete_automated_backups
    error_message = "The short-lived RDS test database must encrypt storage, retain one backup day and be removable without a final snapshot."
  }
  assert {
    condition     = aws_db_instance.moodle.manage_master_user_password && output.moodle_database.master_secret_arn == aws_db_instance.moodle.master_user_secret[0].secret_arn && !contains(keys(output.moodle_database), "password")
    error_message = "Use an RDS-managed secret reference; never put the master password into configuration/output."
  }
  assert {
    condition     = aws_db_parameter_group.moodle.family == "postgres16" && aws_db_instance.moodle.parameter_group_name == aws_db_parameter_group.moodle.name && anytrue([for p in aws_db_parameter_group.moodle.parameter : p.name == "rds.force_ssl" && p.value == "1"]) && output.moodle_database.sslmode == "verify-full"
    error_message = "RDS PostgreSQL must enforce SSL and consumers must verify the server certificate."
  }
  assert {
    condition     = !aws_db_instance.moodle.allow_major_version_upgrade && !aws_db_instance.moodle.apply_immediately && !aws_db_instance.moodle.auto_minor_version_upgrade
    error_message = "Version, capacity and maintenance changes must remain explicit for the experiment baseline."
  }
  assert {
    condition     = aws_db_instance.moodle.instance_class == "db.t4g.micro" && aws_db_instance.moodle.availability_zone == aws_subnet.data_a.availability_zone && !aws_db_instance.moodle.multi_az && !aws_db_instance.moodle.publicly_accessible && output.moodle_database.endpoint == aws_db_instance.moodle.address && !contains(keys(output.moodle_database), "reader_endpoint")
    error_message = "RDS must be a private Single-AZ db.t4g.micro instance with one application endpoint."
  }
  assert {
    condition     = aws_efs_file_system.moodledata.encrypted && aws_efs_file_system.moodledata.availability_zone_name == "" && aws_efs_mount_target.a.subnet_id == aws_subnet.data_a.id && aws_efs_mount_target.b.subnet_id == aws_subnet.data_b.id && aws_efs_mount_target.a.file_system_id == aws_efs_file_system.moodledata.id && aws_efs_mount_target.b.file_system_id == aws_efs_file_system.moodledata.id && aws_efs_mount_target.a.security_groups == toset([aws_security_group.efs_sg.id]) && aws_efs_mount_target.b.security_groups == toset([aws_security_group.efs_sg.id])
    error_message = "Both AZ-local mount targets must serve the same encrypted Regional EFS through EFS SG."
  }
  assert {
    condition     = one(aws_efs_access_point.moodledata.posix_user).uid == var.moodle_efs_uid && one(aws_efs_access_point.moodledata.posix_user).gid == var.moodle_efs_gid && one(aws_efs_access_point.moodledata.root_directory).path == "/moodledata" && one(one(aws_efs_access_point.moodledata.root_directory).creation_info).owner_uid == var.moodle_efs_uid && one(one(aws_efs_access_point.moodledata.root_directory).creation_info).owner_gid == var.moodle_efs_gid && one(one(aws_efs_access_point.moodledata.root_directory).creation_info).permissions == "0770"
    error_message = "EFS access point must isolate moodledata with consistent non-root ownership and permissions."
  }
  assert {
    condition     = one(aws_efs_backup_policy.moodledata.backup_policy).status == "ENABLED" && aws_efs_backup_policy.moodledata.file_system_id == aws_efs_file_system.moodledata.id
    error_message = "Automatic EFS backup must be enabled for the application file system."
  }
  assert {
    condition     = anytrue([for s in jsondecode(aws_efs_file_system_policy.moodledata.policy).Statement : try(s.Effect == "Deny" && s.Condition.Bool["aws:SecureTransport"] == "false" && contains(s.Action, "elasticfilesystem:ClientMount") && contains(s.Action, "elasticfilesystem:ClientWrite"), false)])
    error_message = "EFS policy must explicitly deny non-TLS mounts and writes."
  }
  assert {
    condition     = anytrue([for s in jsondecode(aws_efs_file_system_policy.moodledata.policy).Statement : try(s.Effect == "Allow" && s.Condition.StringEquals["elasticfilesystem:AccessPointArn"] == aws_efs_access_point.moodledata.arn && s.Condition.Bool["aws:SecureTransport"] == "true" && s.Condition.Bool["elasticfilesystem:AccessedViaMountTarget"] == "true" && toset(s.Action) == toset(["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]), false)]) && anytrue([for s in jsondecode(aws_efs_file_system_policy.moodledata.policy).Statement : try(s.Effect == "Deny" && s.Condition.StringNotEquals["elasticfilesystem:AccessPointArn"] == aws_efs_access_point.moodledata.arn, false)]) && anytrue([for s in jsondecode(aws_efs_file_system_policy.moodledata.policy).Statement : s.Effect == "Deny" && s.Action == "elasticfilesystem:ClientRootAccess"])
    error_message = "EFS clients must be restricted to the designated access point without root access."
  }
}

run "custom_runtime_identity" {
  command = plan
  variables {
    moodle_efs_uid = 1001
    moodle_efs_gid = 1001
  }
  assert {
    condition     = one(aws_efs_access_point.moodledata.posix_user).uid == 1001 && one(one(aws_efs_access_point.moodledata.root_directory).creation_info).owner_uid == 1001 && output.moodle_filesystem.uid == 1001 && output.moodle_filesystem.gid == 1001
    error_message = "Changing the runtime identity must update access-point ownership and deployment metadata together."
  }
}

run "reject_wrong_postgres_major" {
  command = plan
  variables { moodle_db_engine_version = "17.1" }
  expect_failures = [var.moodle_db_engine_version]
}

run "reject_non_free_tier_instance_class" {
  command = plan
  variables { moodle_db_instance_class = "db.t4g.medium" }
  expect_failures = [var.moodle_db_instance_class]
}

run "reject_disabled_database_backups" {
  command = plan
  variables { moodle_db_backup_retention_days = 0 }
  expect_failures = [var.moodle_db_backup_retention_days]
}

run "reject_root_uid" {
  command = plan
  variables { moodle_efs_uid = 0 }
  expect_failures = [var.moodle_efs_uid]
}

run "reject_root_gid" {
  command = plan
  variables { moodle_efs_gid = 0 }
  expect_failures = [var.moodle_efs_gid]
}
