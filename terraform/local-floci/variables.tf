variable "floci_endpoint" {
  description = "Loopback-only Floci endpoint; this stack cannot target AWS."
  type        = string
  default     = "http://127.0.0.1:4566"

  validation {
    condition     = contains(["http://127.0.0.1:4566", "http://localhost:4566"], var.floci_endpoint)
    error_message = "floci_endpoint must be the local Floci endpoint on port 4566."
  }
}

variable "moodle_db_password" {
  description = "Local-only master password for the Floci-backed Moodle PostgreSQL container."
  type        = string
  sensitive   = true
  nullable    = false
}

variable "moodle_target_ips" {
  description = "Current Docker IPs of local Moodle web replicas registered behind Floci ALB."
  type        = set(string)
  default     = []
}
