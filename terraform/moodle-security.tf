# No inline ingress/egress: each rule is managed by exactly one resource.
resource "aws_security_group" "alb_sg" {
  name        = "${local.moodle_prefix}-alb-sg"
  description = "Moodle staging alb security boundary"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-alb-sg" })
}

resource "aws_security_group" "moodle_sg" {
  name        = "${local.moodle_prefix}-moodle-sg"
  description = "Moodle staging moodle security boundary"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-moodle-sg" })
}

resource "aws_security_group" "rds_sg" {
  name        = "${local.moodle_prefix}-rds-sg"
  description = "Moodle staging rds security boundary"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-rds-sg" })
}

resource "aws_security_group" "efs_sg" {
  name        = "${local.moodle_prefix}-efs-sg"
  description = "Moodle staging efs security boundary"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-efs-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.alb_sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  description       = "http"
  tags              = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.alb_sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "https"
  tags              = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "alb_to_moodle" {
  security_group_id            = aws_security_group.moodle_sg.id
  referenced_security_group_id = aws_security_group.alb_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  description                  = "alb to moodle"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "monitor_ssh" {
  security_group_id            = aws_security_group.moodle_sg.id
  referenced_security_group_id = aws_security_group.monitor_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 22
  to_port                      = 22
  description                  = "monitor ssh"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "monitor_node_exporter" {
  security_group_id            = aws_security_group.moodle_sg.id
  referenced_security_group_id = aws_security_group.monitor_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 9100
  to_port                      = 9100
  description                  = "monitor node exporter"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "monitor_cadvisor" {
  security_group_id            = aws_security_group.moodle_sg.id
  referenced_security_group_id = aws_security_group.monitor_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 8088
  to_port                      = 8088
  description                  = "monitor cadvisor"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "moodle_to_rds" {
  security_group_id            = aws_security_group.rds_sg.id
  referenced_security_group_id = aws_security_group.moodle_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "moodle to rds"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "monitor_to_rds" {
  security_group_id            = aws_security_group.rds_sg.id
  referenced_security_group_id = aws_security_group.monitor_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "monitor to rds"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_ingress_rule" "moodle_to_efs" {
  security_group_id            = aws_security_group.efs_sg.id
  referenced_security_group_id = aws_security_group.moodle_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  description                  = "moodle to efs"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_egress_rule" "alb_to_moodle" {
  security_group_id            = aws_security_group.alb_sg.id
  referenced_security_group_id = aws_security_group.moodle_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  description                  = "alb to moodle"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_egress_rule" "moodle_to_rds" {
  security_group_id            = aws_security_group.moodle_sg.id
  referenced_security_group_id = aws_security_group.rds_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "moodle to rds"
  tags                         = local.moodle_tags
}

resource "aws_vpc_security_group_egress_rule" "moodle_to_efs" {
  security_group_id            = aws_security_group.moodle_sg.id
  referenced_security_group_id = aws_security_group.efs_sg.id
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  description                  = "moodle to efs"
  tags                         = local.moodle_tags
}

# HTTPS for GHCR/OS repositories through the AZ-local NAT. No blanket egress.
resource "aws_vpc_security_group_egress_rule" "moodle_https" {
  security_group_id = aws_security_group.moodle_sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Pull release images and packages over HTTPS"
  tags              = local.moodle_tags
}
# RDS PostgreSQL/EFS have no initiated outbound access. SG statefulness permits replies.
# VPC DNS and link-local metadata require host-level controls, not SG rules.
