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
agent_image=""

usage() {
  cat >&2 <<'EOF'
Usage:
  bash automation/configure-moodle-monitoring.sh [--agent-image <immutable-ghcr-image>]
  bash automation/configure-moodle-monitoring.sh --syntax-check

Deploys Moodle-only exporters on the two application nodes and Prometheus,
Blackbox Exporter, Alertmanager, and provisioned Grafana on the monitor node.
When --agent-image is supplied, it also deploys Redis, the AI Agent API and its
Celery worker, then configures Alertmanager's internal webhook receiver.
EOF
  exit 64
}

while (( $# > 0 )); do
  case "$1" in
    --syntax-check) syntax_check=true; shift ;;
    --agent-image) [[ $# -ge 2 ]] || usage; agent_image="$2"; shift 2 ;;
    *) usage ;;
  esac
done

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

agent_image_file="$artifacts_dir/moodle-agent-image"
synthetic_password_file="$artifacts_dir/moodle-secrets/moodle-admin-password"
[[ -r "$synthetic_password_file" ]] || {
  echo "Missing root-only Moodle synthetic password file: $synthetic_password_file" >&2
  exit 66
}
if [[ -n "$agent_image" ]]; then
  [[ "$agent_image" =~ ^ghcr\.io/[a-z0-9-]+/moodle-ai-agent:[a-f0-9]{40}$ ]] || {
    echo "Agent image must be an immutable GHCR image tagged by a 40-character commit SHA." >&2
    exit 64
  }
  umask 077
  printf '%s\n' "$agent_image" > "$agent_image_file"
elif [[ -r "$agent_image_file" ]]; then
  agent_image="$(<"$agent_image_file")"
fi

if [[ -n "$agent_image" && ! -r "$artifacts_dir/safe-executor/verifier-ed25519.pub" ]]; then
  echo "Missing Independent Verifier public key; run automation/configure-moodle-safe-executor-api.sh first." >&2
  exit 66
fi

agent_enabled=false
[[ -n "$agent_image" ]] && agent_enabled=true
live_args=(--extra-vars moodle_safe_action_live_enabled=false)
if [[ "${MOODLE_SAFE_ACTION_LIVE_ENABLED:-false}" == true ]]; then
  [[ "${MOODLE_SAFE_ACTION_LIVE_CONFIRM:-}" == staging ]] || {
    echo "Set MOODLE_SAFE_ACTION_LIVE_CONFIRM=staging to enable the Agent live switch." >&2
    exit 77
  }
  live_args=(
    --extra-vars moodle_safe_action_live_enabled=true
    --extra-vars moodle_safe_action_live_confirm=staging
  )
fi

mkdir -p "$artifacts_dir"

# Optional Telegram reporting: copy only the Telegram keys from the operator's
# Git-ignored env file, so laptop-only values (REDIS_HOST, paths) never reach staging.
telegram_args=()
telegram_source="${MOODLE_AGENT_TELEGRAM_ENV_FILE:-$repo_root/release/.env}"
if [[ "$agent_enabled" == true && -r "$telegram_source" ]]; then
  telegram_env_file="$artifacts_dir/agent-telegram.env"
  (umask 077; grep -E '^(TELEGRAM_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_ADMIN_USER_IDS|TELEGRAM_WEBHOOK_SECRET|AI_AGENT_PUBLIC_URL)=.+' \
    "$telegram_source" > "$telegram_env_file" || true)
  if grep -q '^TELEGRAM_TOKEN=' "$telegram_env_file" && grep -q '^TELEGRAM_CHAT_ID=' "$telegram_env_file"; then
    telegram_args=(--extra-vars "moodle_agent_telegram_env_file=$telegram_env_file")
    echo "Agent Telegram reporting: enabled from $telegram_source"
  else
    echo "Agent Telegram reporting: skipped (TELEGRAM_TOKEN or TELEGRAM_CHAT_ID empty in $telegram_source)"
  fi
fi

# The Okta log reader is opt-in and uses a read-only service app. Never place
# its RSA private key in an environment variable or the repository.
okta_args=(--extra-vars moodle_okta_log_enabled=false)
okta_env_file="${MOODLE_OKTA_LOG_ENV_FILE:-$artifacts_dir/okta-sso-reader.env}"
okta_key_file="${MOODLE_OKTA_LOG_PRIVATE_KEY_FILE:-$HOME/.okta/moodle-logs-private.pem}"
if [[ "$agent_enabled" == true && ${#telegram_args[@]} -gt 0 && -r "$okta_env_file" && -r "$okta_key_file" ]]; then
  for name in OKTA_ORG_URL OKTA_API_CLIENT_ID OKTA_API_PRIVATE_KEY_ID OKTA_MOODLE_APP_ID; do
    grep -q "^${name}=[^[:space:]]\+" "$okta_env_file" || {
      echo "Missing $name in $okta_env_file" >&2
      exit 66
    }
  done
  okta_args=(
    --extra-vars moodle_okta_log_enabled=true
    --extra-vars "moodle_okta_log_env_file=$okta_env_file"
    --extra-vars "moodle_okta_log_private_key_file=$okta_key_file"
  )
  echo "Okta SSO log reader: enabled"
else
  echo "Okta SSO log reader: skipped (configuration or key unavailable)"
fi

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
  --extra-vars "moodle_monitor_rds_endpoint=$moodle_rds_endpoint" \
  --extra-vars "moodle_synthetic_password_file=$synthetic_password_file" \
  --extra-vars "moodle_agent_enabled=$agent_enabled" \
  --extra-vars "moodle_agent_image=$agent_image" \
  "${live_args[@]}" \
  "${okta_args[@]}" \
  ${telegram_args[@]+"${telegram_args[@]}"}
