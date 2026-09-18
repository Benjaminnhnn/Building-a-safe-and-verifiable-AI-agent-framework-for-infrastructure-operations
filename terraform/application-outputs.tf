locals {
  moodle_ssh_config_path = abspath("${path.module}/.artifacts/moodle-ssh.config")
}

output "moodle_application" {
  description = "Infrastructure endpoints; targets remain unhealthy until Moodle is deployed."
  value = {
    alb_dns_name     = aws_lb.moodle.dns_name
    alb_arn          = aws_lb.moodle.arn
    target_group_arn = aws_lb_target_group.moodle.arn
    scheme           = var.moodle_certificate_arn == null ? "http" : "https"
    url              = "${var.moodle_certificate_arn == null ? "http" : "https"}://${coalesce(var.moodle_hostname, aws_lb.moodle.dns_name)}"
    nodes = {
      a = { id = aws_instance.moodle_a.id, private_ip = aws_instance.moodle_a.private_ip, cron_enabled = true }
      b = { id = aws_instance.moodle_b.id, private_ip = aws_instance.moodle_b.private_ip, cron_enabled = false }
    }
    legacy_demo_enabled = var.enable_legacy_demo
  }
}

output "moodle_ssh_config" {
  description = "Save to terraform/.artifacts/moodle-ssh.config on the control workstation."
  value = templatefile("${path.module}/templates/moodle-ssh.tftpl", {
    monitor_ip = aws_eip.monitor.public_ip
    app_a_ip   = aws_instance.moodle_a.private_ip
    app_b_ip   = aws_instance.moodle_b.private_ip
    ssh_user   = var.ssh_user
    key_path   = abspath(pathexpand(var.private_key_path))
  })
}

output "moodle_ansible_inventory" {
  description = "Save to terraform/.artifacts/moodle-inventory.yml; requires the SSH config output next to it."
  value = yamlencode({
    all = {
      vars = {
        ansible_user                  = var.ssh_user
        ansible_host_key_checking     = true
        ansible_ssh_host_key_checking = true
        ansible_python_interpreter    = "/usr/bin/python3"
        ansible_ssh_private_key_file  = abspath(pathexpand(var.private_key_path))
        ansible_ssh_common_args       = "-F '${local.moodle_ssh_config_path}'"
      }
      children = {
        monitor = {
          hosts = {
            "monitor-ai-01" = { ansible_host = aws_eip.monitor.public_ip }
          }
        }
        moodle = {
          hosts = {
            "moodle-app-a" = { ansible_host = aws_instance.moodle_a.private_ip, moodle_cron_enabled = true }
            "moodle-app-b" = { ansible_host = aws_instance.moodle_b.private_ip, moodle_cron_enabled = false }
          }
        }
      }
    }
  })
}
