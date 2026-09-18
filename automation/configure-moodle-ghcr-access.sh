#!/usr/bin/env bash
# Configure root-only GHCR pull access on the two Moodle EC2 nodes.
# The token is accepted only from an interactive prompt and is never written to Git.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
ssh_config_path="$repo_root/terraform/.artifacts/moodle-ssh.config"
ghcr_username="${GHCR_USERNAME:-Benjaminnhnn}"
image_ref="${1:-}"
hosts=(moodle-app-a moodle-app-b)

if [[ -z "$image_ref" ]]; then
  echo "Usage: $0 <immutable-ghcr-image-reference>" >&2
  echo "Example: $0 ghcr.io/benjaminnhnn/moodle:<commit-sha>" >&2
  exit 64
fi

if [[ ! -f "$ssh_config_path" ]]; then
  echo "Missing generated SSH config: $ssh_config_path" >&2
  echo "Export it from Terraform before configuring GHCR access." >&2
  exit 66
fi

if [[ ! "$ghcr_username" =~ ^[A-Za-z0-9-]+$ ]]; then
  echo "GHCR_USERNAME must contain only letters, digits, or hyphens." >&2
  exit 64
fi

read -r -s -p "Paste GitHub PAT classic with read:packages: " ghcr_token
printf '\n'
if [[ -z "$ghcr_token" ]]; then
  echo "PAT must not be empty." >&2
  exit 64
fi
trap 'unset ghcr_token' EXIT

for host in "${hosts[@]}"; do
  echo "Configuring root-only GHCR pull access on $host..."
  printf '%s' "$ghcr_token" | ssh -F "$ssh_config_path" "$host" \
    "sudo docker login ghcr.io --username '$ghcr_username' --password-stdin"

  ssh -F "$ssh_config_path" "$host" \
    "sudo test -s /root/.docker/config.json && sudo stat -c '%U:%G %a %n' /root/.docker/config.json"

  ssh -F "$ssh_config_path" "$host" \
    "sudo docker manifest inspect '$image_ref' >/dev/null"
done

echo "GHCR pull access is configured and $image_ref is readable on both Moodle nodes."
