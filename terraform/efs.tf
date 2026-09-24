# No availability_zone_name: this is a Regional file system.
resource "aws_efs_file_system" "moodledata" {
  creation_token   = "${local.moodle_prefix}-moodledata"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  lifecycle {
    prevent_destroy = true
  }


  tags = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-efs" })

}

resource "aws_efs_mount_target" "a" {
  file_system_id  = aws_efs_file_system.moodledata.id
  subnet_id       = aws_subnet.data_a.id
  security_groups = [aws_security_group.efs_sg.id]
}

resource "aws_efs_mount_target" "b" {
  file_system_id  = aws_efs_file_system.moodledata.id
  subnet_id       = aws_subnet.data_b.id
  security_groups = [aws_security_group.efs_sg.id]
}

resource "aws_efs_access_point" "moodledata" {
  file_system_id = aws_efs_file_system.moodledata.id

  posix_user {
    uid = var.moodle_efs_uid
    gid = var.moodle_efs_gid
  }

  root_directory {
    path = "/moodledata"
    creation_info {
      owner_uid   = var.moodle_efs_uid
      owner_gid   = var.moodle_efs_gid
      permissions = "0770"
    }
  }

  tags = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-moodledata" })
}

resource "aws_efs_backup_policy" "moodledata" {
  file_system_id = aws_efs_file_system.moodledata.id
  backup_policy {
    status = "ENABLED"
  }
}

# Network-authorized NFS clients: EFS SG only accepts Moodle SG.
# A future IAM-authenticated mount can additionally narrow the Allow principal.
resource "aws_efs_file_system_policy" "moodledata" {
  file_system_id = aws_efs_file_system.moodledata.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowMoodleAccessPoint"
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action    = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
        Resource  = aws_efs_file_system.moodledata.arn
        Condition = {
          Bool = {
            "aws:SecureTransport"                      = "true"
            "elasticfilesystem:AccessedViaMountTarget" = "true"
          }
          StringEquals = {
            "elasticfilesystem:AccessPointArn" = aws_efs_access_point.moodledata.arn
          }
        }
      },
      {
        Sid       = "DenyUnencryptedTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite", "elasticfilesystem:ClientRootAccess"]
        Resource  = aws_efs_file_system.moodledata.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyOtherAccessPoints"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite", "elasticfilesystem:ClientRootAccess"]
        Resource  = aws_efs_file_system.moodledata.arn
        Condition = {
          StringNotEquals = {
            "elasticfilesystem:AccessPointArn" = aws_efs_access_point.moodledata.arn
          }
        }
      },
      {
        Sid       = "DenyRootAccess"
        Effect    = "Deny"
        Principal = "*"
        Action    = "elasticfilesystem:ClientRootAccess"
        Resource  = aws_efs_file_system.moodledata.arn
      }
    ]
  })
}
