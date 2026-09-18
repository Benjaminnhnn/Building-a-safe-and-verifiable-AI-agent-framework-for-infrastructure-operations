variable "enable_legacy_demo" {
  description = "Keep existing payment EC2/EIPs by default. Set false only for a new Moodle environment or a reviewed decommission."
  type        = bool
  default     = true
}

variable "monitor_use_management_subnet" {
  description = "New deployments can put monitor in management subnet. Changing this replaces an existing monitor EC2."
  type        = bool
  default     = false
}

variable "monitor_root_volume_size" {
  description = "Optional monitor disk override; null preserves the existing root_volume_size."
  type        = number
  default     = null
  validation {
    condition     = var.monitor_root_volume_size == null ? true : var.monitor_root_volume_size >= 20 && floor(var.monitor_root_volume_size) == var.monitor_root_volume_size
    error_message = "Monitor disk override must be an integer of at least 20 GiB."
  }
}

variable "moodle_instance_type" {
  type    = string
  default = "t3.small"
}

variable "moodle_root_volume_size" {
  description = "Encrypted root disk with room for current/previous release images and deploy preflight."
  type        = number
  default     = 30
  validation {
    condition     = var.moodle_root_volume_size >= 30 && floor(var.moodle_root_volume_size) == var.moodle_root_volume_size
    error_message = "Use at least 30 GiB for Moodle hosts and rollback images."
  }
}

variable "ec2_ami_id" {
  description = "Pin the Amazon Linux 2023 x86_64 AMI for repeatable deploys; null uses the existing public AMI lookup."
  type        = string
  default     = null
  validation {
    condition     = var.ec2_ami_id == null ? true : can(regex("^ami-[0-9a-f]{8}([0-9a-f]{9})?$", var.ec2_ami_id))
    error_message = "Supply a valid AMI ID for the selected AWS region."
  }
}

variable "moodle_certificate_arn" {
  description = "Issued ACM certificate in the ALB region. Null requires explicit HTTP smoke-test opt-in."
  type        = string
  default     = null
  validation {
    condition     = var.moodle_certificate_arn == null ? true : can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/.+$", var.moodle_certificate_arn))
    error_message = "Use an ACM certificate ARN or null."
  }
}

variable "moodle_allow_http" {
  description = "Explicit opt-in for HTTP-only staging infrastructure smoke tests; use HTTPS before login/data tests."
  type        = bool
  default     = false
}

variable "moodle_hostname" {
  description = "Optional DNS hostname covered by the certificate; create its ALIAS/CNAME to the ALB separately."
  type        = string
  default     = null
  validation {
    condition     = var.moodle_hostname == null ? true : can(regex("^[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]$", var.moodle_hostname))
    error_message = "Supply a DNS hostname without a scheme, port or path."
  }
}
