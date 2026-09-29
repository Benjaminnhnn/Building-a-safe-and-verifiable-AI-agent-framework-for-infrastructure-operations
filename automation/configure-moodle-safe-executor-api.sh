#!/usr/bin/env bash
# Create local-only signing/transport material, then install the private API.
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
terraform_dir="$root/terraform"
artifact_dir="$terraform_dir/.artifacts"
secrets_dir="$artifact_dir/safe-executor"
inventory="$artifact_dir/moodle-inventory.yml"

umask 077
mkdir -p "$secrets_dir"

if [[ ! -e "$secrets_dir/agent-hmac.key" ]]; then
  openssl rand -hex 32 > "$secrets_dir/agent-hmac.key"
fi
if [[ ! -e "$secrets_dir/approval-ed25519-private.pem" ]]; then
  openssl genpkey -algorithm ED25519 -out "$secrets_dir/approval-ed25519-private.pem"
fi
if [[ ! -e "$secrets_dir/approval-ed25519.pub" ]]; then
  openssl pkey -in "$secrets_dir/approval-ed25519-private.pem" -pubout \
    -out "$secrets_dir/approval-ed25519.pub"
fi
chmod 0600 "$secrets_dir/agent-hmac.key" "$secrets_dir/approval-ed25519-private.pem"
chmod 0644 "$secrets_dir/approval-ed25519.pub"

terraform -chdir="$terraform_dir" output -raw moodle_ansible_inventory > "$inventory"
chmod 0600 "$inventory"
ansible_args=()
if [[ "${SAFE_EXECUTOR_LIVE_ENABLED:-false}" == true ]]; then
  [[ "${SAFE_EXECUTOR_LIVE_CONFIRM:-}" == staging ]] || {
    echo "Set SAFE_EXECUTOR_LIVE_CONFIRM=staging to enable the live API kill switch." >&2
    exit 77
  }
  ansible_args+=(--extra-vars executor_live_enabled=true)
fi
ansible-playbook -i "$inventory" \
  "$root/ansible/playbooks/configure-moodle-safe-executor-api.yml" \
  "${ansible_args[@]}"

printf 'Safe Executor API installed. Live execution remains disabled.\n'
printf 'Operator private key (keep local; never upload): %s\n' \
  "$secrets_dir/approval-ed25519-private.pem"
