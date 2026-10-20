#!/usr/bin/env bash
# Roll out a prebuilt immutable Moodle image. Database-role provisioning and
# secret staging are reviewed one-time operations, never implicit CD actions.
set -euo pipefail

required_vars=(
  MOODLE_IMAGE
  MOODLE_APP_A_HOST
  MOODLE_APP_B_HOST
  SSH_PRIVATE_KEY
  GHCR_USERNAME
  GHCR_TOKEN
)

for variable in "${required_vars[@]}"; do
  if [[ -z "${!variable:-}" ]]; then
    echo "Missing required environment variable: $variable" >&2
    exit 2
  fi
done

[[ "$MOODLE_IMAGE" =~ ^ghcr\.io/[a-z0-9-]+/moodle:[a-f0-9]{40}$ ]] || {
  echo "MOODLE_IMAGE must be an immutable ghcr.io/<owner>/moodle:<40-char-sha> reference." >&2
  exit 2
}

ssh_user="${SSH_USER:-ec2-user}"
ssh_port="${SSH_PORT:-22}"
[[ "$ssh_port" =~ ^[1-9][0-9]{0,4}$ ]] || {
  echo "SSH_PORT must be a TCP port number." >&2
  exit 2
}

runner_temp="${RUNNER_TEMP:-/tmp}"
key_path="$runner_temp/moodle-deploy-key-${RANDOM}${RANDOM}"
umask 077
printf '%s\n' "$SSH_PRIVATE_KEY" > "$key_path"
chmod 600 "$key_path"
trap 'shred -u "$key_path" 2>/dev/null || true' EXIT

ssh_base=(
  ssh
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
  -i "$key_path"
  -p "$ssh_port"
)

# remote_deploy <host> <cron|no-cron>
# Exactly one node may run moodle-cron, otherwise Moodle scheduled tasks run
# twice. Naming moodle-cron on the command line starts it even though it sits
# in the "cron" profile, so the service list is chosen per node here.
remote_deploy() {
  local host="$1" cron_mode="$2"
  local quoted_image quoted_user quoted_token
  [[ "$cron_mode" == "cron" || "$cron_mode" == "no-cron" ]] || {
    echo "remote_deploy: cron mode must be cron or no-cron" >&2
    return 2
  }
  printf -v quoted_image '%q' "$MOODLE_IMAGE"
  printf -v quoted_user '%q' "$GHCR_USERNAME"
  printf -v quoted_token '%q' "$GHCR_TOKEN"

  {
    printf 'MOODLE_IMAGE=%s\n' "$quoted_image"
    printf 'GHCR_USERNAME=%s\n' "$quoted_user"
    printf 'GHCR_TOKEN=%s\n' "$quoted_token"
    printf 'CRON_MODE=%s\n' "$cron_mode"
    cat <<'REMOTE'
set -euo pipefail

runtime_env=/opt/moodle/release/moodle-runtime.env
compose_file=/opt/moodle/release/docker-compose.yml
sudo test -r "$runtime_env" || {
  echo "Moodle runtime has not been staged: $runtime_env" >&2
  exit 66
}
sudo test -r "$compose_file" || {
  echo "Moodle Compose file has not been staged: $compose_file" >&2
  exit 66
}

printf '%s' "$GHCR_TOKEN" | sudo docker login ghcr.io --username "$GHCR_USERNAME" --password-stdin >/dev/null

if sudo grep -q '^MOODLE_IMAGE=' "$runtime_env"; then
  sudo sed -i "s|^MOODLE_IMAGE=.*|MOODLE_IMAGE=$MOODLE_IMAGE|" "$runtime_env"
else
  printf 'MOODLE_IMAGE=%s\n' "$MOODLE_IMAGE" | sudo tee -a "$runtime_env" >/dev/null
fi

if [[ "$CRON_MODE" == "cron" ]]; then
  services=(moodle-web moodle-cron)
else
  services=(moodle-web)
  # Remove a cron runner left on this node by an earlier rollout.
  sudo docker compose --env-file "$runtime_env" -f "$compose_file" rm --stop --force moodle-cron
fi

sudo docker compose --env-file "$runtime_env" -f "$compose_file" pull "${services[@]}"
sudo docker compose --env-file "$runtime_env" -f "$compose_file" up -d --wait "${services[@]}"
curl --fail --silent --show-error --retry 6 --retry-delay 5 http://127.0.0.1:8080/healthz.php >/dev/null
sudo docker compose --env-file "$runtime_env" -f "$compose_file" ps
REMOTE
  } | "${ssh_base[@]}" "${ssh_user}@${host}" 'bash -s'
}

# Keep one healthy target behind the ALB while the other is updated. Node A is
# the single cron runner (Terraform output moodle_application.nodes.a.cron_enabled).
remote_deploy "$MOODLE_APP_B_HOST" no-cron
remote_deploy "$MOODLE_APP_A_HOST" cron

if [[ -n "${MOODLE_PUBLIC_URL:-}" ]]; then
  public_health_url="${MOODLE_PUBLIC_URL%/}/healthz.php"
  curl --fail --silent --show-error --retry 12 --retry-delay 5 "$public_health_url" >/dev/null
  echo "ALB health check passed: $public_health_url"
fi

echo "Moodle rollout completed successfully: $MOODLE_IMAGE"
