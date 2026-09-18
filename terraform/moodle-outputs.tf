output "moodle_network" {
  description = "Subnet, route and SG IDs for the Moodle RDS PostgreSQL, EFS, EC2 and ALB resources."
  value = {
    vpc_id               = aws_vpc.main.id
    public_subnet_ids    = { a = aws_subnet.public_a.id, b = aws_subnet.public_b.id }
    app_subnet_ids       = { a = aws_subnet.app_a.id, b = aws_subnet.app_b.id }
    data_subnet_ids      = { a = aws_subnet.data_a.id, b = aws_subnet.data_b.id }
    management_subnet_id = aws_subnet.management.id
    availability_zones   = { a = aws_subnet.app_a.availability_zone, b = aws_subnet.app_b.availability_zone }
    nat_gateway_ids      = { a = aws_nat_gateway.a.id, b = aws_nat_gateway.b.id }
    security_group_ids = {
      alb     = aws_security_group.alb_sg.id
      moodle  = aws_security_group.moodle_sg.id
      rds     = aws_security_group.rds_sg.id
      efs     = aws_security_group.efs_sg.id
      monitor = aws_security_group.monitor_sg.id
    }
  }
}
