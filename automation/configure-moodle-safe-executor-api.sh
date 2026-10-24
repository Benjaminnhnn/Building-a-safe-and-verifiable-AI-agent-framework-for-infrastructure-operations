#!/usr/bin/env bash
# Create transport material and validate separately provisioned public keys.
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
if [[ ! -e "$secrets_dir/safe-action-api-token" ]]; then
  openssl rand -hex 32 > "$secrets_dir/safe-action-api-token"
fi
for public_key in approval-ed25519.pub gate-approval-ed25519.pub verifier-ed25519.pub; do
  [[ -r "$secrets_dir/$public_key" ]] || {
    echo "Missing $secrets_dir/$public_key; provision public keys from their separate approver/verifier owners." >&2
    exit 66
  }
  grep -q 'BEGIN PUBLIC KEY' "$secrets_dir/$public_key" || {
    echo "Invalid public key: $secrets_dir/$public_key" >&2
    exit 65
  }
done
for private_key in approval-ed25519-private.pem gate-approval-ed25519-private.pem verifier-ed25519-private.pem; do
  [[ ! -e "$secrets_dir/$private_key" ]] || {
    echo "Private key is present on the provisioning host: $secrets_dir/$private_key; move it to its owner before continuing." >&2
    exit 65
  }
done
if cmp -s "$secrets_dir/approval-ed25519.pub" "$secrets_dir/gate-approval-ed25519.pub" \
  || cmp -s "$secrets_dir/approval-ed25519.pub" "$secrets_dir/verifier-ed25519.pub" \
  || cmp -s "$secrets_dir/gate-approval-ed25519.pub" "$secrets_dir/verifier-ed25519.pub"; then
  echo "Approver and verifier public keys must be distinct." >&2
  exit 65
fi
chmod 0600 "$secrets_dir/agent-hmac.key" "$secrets_dir/safe-action-api-token"
chmod 0644 "$secrets_dir/approval-ed25519.pub" "$secrets_dir/gate-approval-ed25519.pub" \
  "$secrets_dir/verifier-ed25519.pub"

terraform -chdir="$terraform_dir" output -raw moodle_ansible_inventory > "$inventory"
chmod 0600 "$inventory"
ansible_args=()
if [[ "${SAFE_EXECUTOR_LIVE_ENABLED:-false}" == true ]]; then
  [[ "${SAFE_EXECUTOR_LIVE_CONFIRM:-}" == staging ]] || {
    echo "Set SAFE_EXECUTOR_LIVE_CONFIRM=staging to enable the live API kill switch." >&2
    exit 77
  }
  ansible_args+=(--extra-vars executor_live_enabled=true --extra-vars executor_live_confirm=staging)
fi
ansible-playbook -i "$inventory" \
  "$root/ansible/playbooks/configure-moodle-safe-executor-api.yml" \
  ${ansible_args[@]+"${ansible_args[@]}"}

if [[ "${SAFE_EXECUTOR_LIVE_ENABLED:-false}" == true ]]; then
  printf 'Safe Executor API installed with live execution enabled for the confirmed staging drill.\n'
else
  printf 'Safe Executor API installed. Live execution remains disabled.\n'
fi
printf 'Approval public keys validated; keep each approver private key with its owner.\n'
printf 'Verifier private key must remain with the independent verifier.\n'
printf 'Agent API bearer token (keep local): %s\n' \
  "$secrets_dir/safe-action-api-token"
