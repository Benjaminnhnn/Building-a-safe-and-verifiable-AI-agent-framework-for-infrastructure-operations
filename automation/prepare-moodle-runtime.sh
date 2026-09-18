#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
artifacts_dir="$terraform_dir/.artifacts"
inventory_path="$artifacts_dir/moodle-inventory.yml"
ssh_config_path="$artifacts_dir/moodle-ssh.config"
filesystem_vars_path="$artifacts_dir/moodle-filesystem.vars.json"

for required_command in terraform ansible-playbook; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "Required command not found: $required_command" >&2
    exit 127
  fi
done

umask 077
mkdir -p "$artifacts_dir"

terraform -chdir="$terraform_dir" output -raw moodle_ssh_config > "$ssh_config_path"
terraform -chdir="$terraform_dir" output -raw moodle_ansible_inventory > "$inventory_path"
terraform -chdir="$terraform_dir" output -json moodle_filesystem > "$filesystem_vars_path"

ansible-playbook \
  -i "$inventory_path" \
  -e "@$filesystem_vars_path" \
  "$repo_root/ansible/playbooks/prepare-moodle-runtime.yml" \
  "$@"
