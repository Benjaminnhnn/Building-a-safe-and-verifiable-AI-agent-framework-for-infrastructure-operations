# Read-only instance role for the SEC-01 RDS security-group audit on
# monitor-ai-01 (ansible/files/moodle-monitoring/moodle-rds-ingress-audit.sh).
# It can describe security-group rules and nothing else; remediation of the
# RDS security group stays human-only.
resource "aws_iam_role" "monitor_audit" {
  name = "${var.project_name}-${var.environment}-monitor-audit"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = local.common_tags
}

# DescribeSecurityGroupRules does not support resource-level permissions.
resource "aws_iam_role_policy" "monitor_audit" {
  name = "read-only-rds-ingress-audit"
  role = aws_iam_role.monitor_audit.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadOnlySecurityGroupRules"
      Effect   = "Allow"
      Action   = "ec2:DescribeSecurityGroupRules"
      Resource = "*"
    }]
  })
}

resource "aws_iam_instance_profile" "monitor_audit" {
  name = "${var.project_name}-${var.environment}-monitor-audit"
  role = aws_iam_role.monitor_audit.name
  tags = local.common_tags
}
