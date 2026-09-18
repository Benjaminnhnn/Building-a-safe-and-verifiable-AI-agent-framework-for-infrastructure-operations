output "moodle_database" {
  description = "RDS PostgreSQL connection metadata. No password is read into Terraform state or outputs."
  value = {
    instance_identifier = aws_db_instance.moodle.identifier
    endpoint            = aws_db_instance.moodle.address
    address             = aws_db_instance.moodle.address
    port                = aws_db_instance.moodle.port
    database_name       = "moodle"
    master_secret_arn   = try(aws_db_instance.moodle.master_user_secret[0].secret_arn, null)
    sslmode             = "verify-full"
  }
}

output "moodle_filesystem" {
  description = "EFS metadata for Ansible bootstrap and the Moodle release mount."
  value = {
    file_system_id    = aws_efs_file_system.moodledata.id
    access_point_id   = aws_efs_access_point.moodledata.id
    mount_target_ids  = { a = aws_efs_mount_target.a.id, b = aws_efs_mount_target.b.id }
    access_point_path = "/moodledata"
    host_mount_path   = "/mnt/efs/moodledata"
    mount_options     = "tls,accesspoint=${aws_efs_access_point.moodledata.id},_netdev"
    uid               = var.moodle_efs_uid
    gid               = var.moodle_efs_gid
  }
}
