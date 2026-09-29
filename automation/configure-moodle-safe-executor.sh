#!/usr/bin/env bash
# Provision only the restricted SSH executor identity and forced wrapper.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
terraform_dir="$root/terraform"
artifacts="$terraform_dir/.artifacts"
inventory="$artifacts/moodle-inventory.yml"
terraform -chdir="$terraform_dir" output -raw moodle_ansible_inventory > "$inventory"
ansible-playbook -i "$inventory" "$root/ansible/playbooks/configure-moodle-safe-executor.yml"
