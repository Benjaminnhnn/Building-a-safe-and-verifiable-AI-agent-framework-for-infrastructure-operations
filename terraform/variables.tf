variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project_name" {
  type    = string
  default = "aiops-bank"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "my_ip_cidr" {
  type        = string
  description = "IP public CIDR"
  validation {
    condition     = can(cidrnetmask(var.my_ip_cidr)) && can(regex("/(2[4-9]|3[0-2])$", var.my_ip_cidr))
    error_message = "Use an administrator IPv4 CIDR with prefix /24 through /32; public-wide access is forbidden."
  }
}

variable "ci_cd_ssh_cidr_blocks" {
  type        = list(string)
  description = "Additional CIDR blocks allowed to SSH from CI/CD runners"
  default     = []
  validation {
    condition     = alltrue([for cidr in var.ci_cd_ssh_cidr_blocks : can(cidrnetmask(cidr)) && can(regex("/(2[4-9]|3[0-2])$", cidr))])
    error_message = "Each CI SSH IPv4 CIDR must use prefix /24 through /32; do not allow all Internet runners."
  }
}

variable "ai_engineer_monitor_ssh_cidr_blocks" {
  type        = list(string)
  description = "Additional AI Engineer IPv4 CIDRs allowed to SSH only to the monitor node"
  default     = []
  validation {
    condition     = alltrue([for cidr in var.ai_engineer_monitor_ssh_cidr_blocks : can(cidrnetmask(cidr)) && can(regex("/(2[4-9]|3[0-2])$", cidr))])
    error_message = "Each AI Engineer SSH IPv4 CIDR must use prefix /24 through /32; do not allow all Internet clients."
  }
}

variable "moodle_project_name" {
  type        = string
  default     = "moodle"
  description = "Prefix for new Moodle resources; preserves legacy project_name resources."
}

variable "moodle_environment" {
  type    = string
  default = "staging"
  validation {
    condition     = var.moodle_environment == "staging"
    error_message = "Sprint 1 Moodle infrastructure is staging-only."
  }
}

variable "public_key_path" {
  type        = string
  description = "The path direct to file public key"
}

variable "private_key_path" {
  type        = string
  description = "The path direct to private key"
}

variable "ssh_user" {
  type    = string
  default = "ec2-user"
}

variable "monitor_instance_type" {
  type    = string
  default = "t3.small"
}

variable "web_instance_type" {
  type    = string
  default = "t2.micro"
}

variable "core_instance_type" {
  type    = string
  default = "t2.micro"
}

variable "root_volume_size" {
  type    = number
  default = 12
}
