output "monitor_public_ip" {
  value       = aws_eip.monitor.public_ip
  description = "Elastic IP of monitor instance (static)"
}

output "monitor_private_ip" {
  value = aws_instance.monitor.private_ip
}

