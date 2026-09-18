resource "aws_db_subnet_group" "moodle" {
  name        = "${local.moodle_prefix}-db-subnets"
  description = "Private Moodle database subnets across two Availability Zones"
  subnet_ids  = [aws_subnet.data_a.id, aws_subnet.data_b.id]
  tags        = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-db-subnets" })
}

resource "aws_db_parameter_group" "moodle" {
  name        = "${local.moodle_prefix}-postgres16"
  family      = "postgres16"
  description = "Moodle RDS PostgreSQL transport security"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  tags = local.moodle_tags
}

resource "aws_cloudwatch_log_group" "moodle_postgresql" {
  name              = "/aws/rds/instance/${local.moodle_prefix}-postgres/postgresql"
  retention_in_days = 14
  tags              = local.moodle_tags
}

# RDS PostgreSQL is deliberately Single-AZ and db.t4g.micro so the environment
# remains suitable for a short Free Tier/credit-backed test. The subnet group
# still spans two data AZs because RDS requires that topology for VPC placement.
resource "aws_db_instance" "moodle" {
  identifier                      = "${local.moodle_prefix}-postgres"
  engine                          = "postgres"
  engine_version                  = var.moodle_db_engine_version
  instance_class                  = var.moodle_db_instance_class
  allocated_storage               = 20
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = "moodle"
  username                        = "moodle_admin"
  manage_master_user_password     = true
  port                            = 5432
  availability_zone               = aws_subnet.data_a.availability_zone
  multi_az                        = false
  publicly_accessible             = false
  db_subnet_group_name            = aws_db_subnet_group.moodle.name
  vpc_security_group_ids          = [aws_security_group.rds_sg.id]
  parameter_group_name            = aws_db_parameter_group.moodle.name
  backup_retention_period         = var.moodle_db_backup_retention_days
  backup_window                   = "18:00-19:00"
  maintenance_window              = "sat:19:00-sat:20:00"
  auto_minor_version_upgrade      = false
  allow_major_version_upgrade     = false
  apply_immediately               = false
  deletion_protection             = false
  skip_final_snapshot             = true
  delete_automated_backups        = true
  copy_tags_to_snapshot           = false
  enabled_cloudwatch_logs_exports = ["postgresql"]

  depends_on = [aws_cloudwatch_log_group.moodle_postgresql]

  tags = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-postgres" })
}
