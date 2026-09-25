#!/usr/bin/env bash
# Grant a supplied public key access to monitor-ai-01 only.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
artifacts_dir="$terraform_dir/.artifacts"
key_path="${1:-$HOME/.ssh/ai-engineer.pub}"

if [[ ! -r "$key_path" ]]; then
  echo "Readable AI Engineer public key not found: $key_path" >&2
  exit 66
fi

if ! head -n 1 "$key_path" | grep -Eq '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-)'; then
  echo "Expected an OpenSSH public key, not a private key: $key_path" >&2
  exit 64
fi

for required_command in ansible-playbook terraform; do
  command -v "$required_command" >/dev/null 2>&1 || {
    echo "Required command not found: $required_command" >&2
    exit 127
  }
done

umask 077
mkdir -p "$artifacts_dir"
terraform -chdir="$terraform_dir" output -raw moodle_ssh_config > "$artifacts_dir/moodle-ssh.config"
terraform -chdir="$terraform_dir" output -raw moodle_ansible_inventory > "$artifacts_dir/moodle-inventory.yml"
chmod 0600 "$artifacts_dir/moodle-ssh.config"

ansible-playbook \
  -i "$artifacts_dir/moodle-inventory.yml" \
  "$repo_root/ansible/playbooks/authorize-ai-engineer.yml" \
  --extra-vars "ai_engineer_public_key_path=$key_path"
