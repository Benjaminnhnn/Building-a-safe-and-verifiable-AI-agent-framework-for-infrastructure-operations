output "vpc_id" {
  value = aws_vpc.local.id
}

output "subnet_id" {
  value = aws_subnet.workloads.id
}

output "security_group_id" {
  value = aws_security_group.workloads.id
}

output "iam_role_arn" {
  value = aws_iam_role.observer.arn
}

output "local_ec2_instance_id" {
  value = aws_instance.control_plane_probe.id
}

output "moodle_database_address" {
  description = "Floci RDS proxy hostname for local Docker workloads."
  value       = aws_db_instance.moodle.address
}

output "moodle_database_port" {
  description = "Floci-published local PostgreSQL proxy port."
  value       = aws_db_instance.moodle.port
}

output "moodle_load_balancer_dns_name" {
  value = aws_lb.moodle.dns_name
}

output "moodle_local_url" {
  value = "http://127.0.0.1:18081"
}
