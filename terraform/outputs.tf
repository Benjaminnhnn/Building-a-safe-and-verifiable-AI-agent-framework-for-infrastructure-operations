output "monitor_public_ip" {
  value       = aws_eip.monitor.public_ip
  description = "Elastic IP of monitor instance (static)"
}

output "web_public_ip" {
  value       = try(aws_eip.web[0].public_ip, "")
  description = "Elastic IP of web instance (static)"
}

output "core_public_ip" {
  value       = try(aws_eip.core[0].public_ip, "")
  description = "Elastic IP of core instance (static)"
}

output "monitor_private_ip" {
  value = aws_instance.monitor.private_ip
}

output "web_private_ip" {
  value = try(aws_instance.web[0].private_ip, "")
}

output "core_private_ip" {
  value = try(aws_instance.core[0].private_ip, "")
}

output "ssh_commands" {
  value = <<-EOT
  SSH monitor:
  ssh -i "${abspath(pathexpand(var.private_key_path))}" ${var.ssh_user}@${aws_eip.monitor.public_ip}

  %{if var.enable_legacy_demo}SSH web:
  ssh -i "${abspath(pathexpand(var.private_key_path))}" ${var.ssh_user}@${try(aws_eip.web[0].public_ip, "")}

  SSH core:
  ssh -i "${abspath(pathexpand(var.private_key_path))}" ${var.ssh_user}@${try(aws_eip.core[0].public_ip, "")}
  %{endif}
  EOT
}

output "ansible_inventory" {
  value = <<-EOT
  [monitor]
  monitor-ai-01 ansible_host=${aws_eip.monitor.public_ip} private_ip=${aws_instance.monitor.private_ip} ansible_user=${var.ssh_user}

  %{if var.enable_legacy_demo}[web]
  bank-web-01 ansible_host=${try(aws_eip.web[0].public_ip, "")} private_ip=${try(aws_instance.web[0].private_ip, "")} ansible_user=${var.ssh_user}

  [core]
  bank-core-01 ansible_host=${try(aws_eip.core[0].public_ip, "")} private_ip=${try(aws_instance.core[0].private_ip, "")} ansible_user=${var.ssh_user}

  [app:children]
  web
  core

  %{endif}
  [all:vars]
  ansible_python_interpreter=/usr/bin/python3
  ansible_ssh_private_key_file=${abspath(pathexpand(var.private_key_path))}
  EOT
}

output "elastic_ips" {
  value = {
    monitor_eip = aws_eip.monitor.public_ip
    web_eip     = try(aws_eip.web[0].public_ip, "")
    core_eip    = try(aws_eip.core[0].public_ip, "")
  }
  description = "Elastic IPs (static public IPs) for each instance"
}

output "elastic_ips_info" {
  value = <<-EOT
  ✓ Elastic IPs have been allocated (Static Public IPs)

  Monitor: ${aws_eip.monitor.public_ip} (Allocation ID: ${aws_eip.monitor.id})
  Web:     ${try(aws_eip.web[0].public_ip, "")} (Allocation ID: ${try(aws_eip.web[0].id, "")})
  Core:    ${try(aws_eip.core[0].public_ip, "")} (Allocation ID: ${try(aws_eip.core[0].id, "")})

  Note: These IPs will NOT change when instances are stopped/started!
  EOT
}
