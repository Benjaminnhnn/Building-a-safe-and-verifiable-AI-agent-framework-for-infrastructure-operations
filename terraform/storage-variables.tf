variable "moodle_db_engine_version" {
  description = "Pinned RDS PostgreSQL 16 engine version. Verify regional availability before changing."
  type        = string
  default     = "16.10"
  validation {
    condition     = can(regex("^16\\.[0-9]+$", var.moodle_db_engine_version))
    error_message = "Use an exact RDS PostgreSQL 16 version compatible with postgres16."
  }
}

variable "moodle_db_instance_class" {
  description = "RDS PostgreSQL instance class. The default is suitable for a short Free Tier/credit-backed test."
  type        = string
  default     = "db.t4g.micro"
  validation {
    condition     = contains(["db.t3.micro", "db.t4g.micro"], var.moodle_db_instance_class)
    error_message = "Use a Free Tier-eligible RDS class: db.t3.micro or db.t4g.micro."
  }
}

variable "moodle_db_backup_retention_days" {
  type    = number
  default = 1
  validation {
    condition     = var.moodle_db_backup_retention_days >= 1 && var.moodle_db_backup_retention_days <= 35 && floor(var.moodle_db_backup_retention_days) == var.moodle_db_backup_retention_days
    error_message = "Keep automated database backups for an integer from 1 to 35 days."
  }
}

variable "moodle_efs_uid" {
  description = "Non-root runtime UID; the Moodle image and EFS client configuration must match."
  type        = number
  default     = 1000
  validation {
    condition     = var.moodle_efs_uid > 0 && var.moodle_efs_uid <= 65534 && floor(var.moodle_efs_uid) == var.moodle_efs_uid
    error_message = "Use a non-root integer UID from 1 to 65534."
  }
}

variable "moodle_efs_gid" {
  description = "Runtime GID paired with the Moodle UID."
  type        = number
  default     = 1000
  validation {
    condition     = var.moodle_efs_gid > 0 && var.moodle_efs_gid <= 65534 && floor(var.moodle_efs_gid) == var.moodle_efs_gid
    error_message = "Use a non-root integer GID from 1 to 65534."
  }
}
