#!/usr/bin/env bash
# Deploy isolated Moodle observability using only Terraform-derived targets.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
artifacts_dir="$terraform_dir/.artifacts"
inventory_path="$artifacts_dir/moodle-inventory.yml"
ssh_config_path="$artifacts_dir/moodle-ssh.config"
playbook_path="$repo_root/ansible/playbooks/configure-moodle-monitoring.yml"
syntax_check=false

usage() {
  cat >&2 <<'EOF'
Usage: bash automation/configure-moodle-monitoring.sh [--syntax-check]

Deploys Moodle-only exporters on the two application nodes and Prometheus,
Blackbox Exporter, Alertmanager, and provisioned Grafana on the monitor node.
It does not change the Moodle release Compose project, RDS, EFS, or external
alert receivers.
EOF
  exit 64
}

if (( $# == 1 )) && [[ "$1" == "--syntax-check" ]]; then
  syntax_check=true
elif (( $# != 0 )); then
  usage
fi

for required_command in ansible-playbook jq terraform; do
  command -v "$required_command" >/dev/null 2>&1 || {
    echo "Required command not found: $required_command" >&2
    exit 127
  }
done

[[ -f "$playbook_path" ]] || {
  echo "Monitoring playbook is missing: $playbook_path" >&2
  exit 66
}

if [[ "$syntax_check" == true ]]; then
  ansible-playbook --syntax-check "$playbook_path"
  exit 0
fi

mkdir -p "$artifacts_dir"
terraform -chdir="$terraform_dir" output -raw moodle_ssh_config > "$ssh_config_path"
terraform -chdir="$terraform_dir" output -raw moodle_ansible_inventory > "$inventory_path"
chmod 0600 "$ssh_config_path"

application_json="$(terraform -chdir="$terraform_dir" output -json moodle_application)"
database_json="$(terraform -chdir="$terraform_dir" output -json moodle_database)"
moodle_public_url="$(jq -r '.url' <<<"$application_json")"
moodle_rds_endpoint="$(jq -r '.endpoint' <<<"$database_json")"

[[ "$moodle_public_url" =~ ^https?:// ]] || {
  echo "Terraform did not return a valid Moodle public URL." >&2
  exit 65
}
[[ "$moodle_rds_endpoint" != "null" && -n "$moodle_rds_endpoint" ]] || {
  echo "Terraform did not return a Moodle RDS endpoint." >&2
  exit 65
}

ansible-playbook \
  -i "$inventory_path" \
  "$playbook_path" \
  --extra-vars "moodle_monitor_public_url=$moodle_public_url" \
  --extra-vars "moodle_monitor_rds_endpoint=$moodle_rds_endpoint"
