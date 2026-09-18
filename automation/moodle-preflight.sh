#!/usr/bin/env bash
# Read-only deployment readiness check for the Moodle release.
# It intentionally does not create secrets, log in to GHCR, or start containers.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
inventory_path="$terraform_dir/.artifacts/moodle-inventory.yml"
aws_profile="${AWS_PROFILE:-target-account}"
image_ref="${1:-}"

if [[ -z "$image_ref" ]]; then
  echo "Usage: $0 <immutable-ghcr-image-reference>" >&2
  echo "Example: $0 ghcr.io/owner/moodle:<commit-sha>" >&2
  exit 64
fi

for required_command in ansible aws jq terraform; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "Required command not found: $required_command" >&2
    exit 127
  fi
done

if [[ ! -f "$inventory_path" ]]; then
  echo "Missing generated inventory: $inventory_path" >&2
  echo "Run bash automation/prepare-moodle-runtime.sh before this preflight." >&2
  exit 66
fi

application_json="$(terraform -chdir="$terraform_dir" output -json moodle_application)"
database_json="$(terraform -chdir="$terraform_dir" output -json moodle_database)"
filesystem_json="$(terraform -chdir="$terraform_dir" output -json moodle_filesystem)"

region="$(jq -r '.alb_arn | split(":")[3]' <<<"$application_json")"
db_identifier="$(jq -r '.instance_identifier' <<<"$database_json")"
target_group_arn="$(jq -r '.target_group_arn' <<<"$application_json")"
file_system_id="$(jq -r '.file_system_id' <<<"$filesystem_json")"

aws sts get-caller-identity --profile "$aws_profile" --output json >/dev/null

db_status="$(aws rds describe-db-instances \
  --profile "$aws_profile" \
  --region "$region" \
  --db-instance-identifier "$db_identifier" \
  --query 'DBInstances[0].DBInstanceStatus' \
  --output text)"
if [[ "$db_status" != "available" ]]; then
  echo "RDS instance $db_identifier is not available (status: $db_status)." >&2
  exit 1
fi

efs_state="$(aws efs describe-file-systems \
  --profile "$aws_profile" \
  --region "$region" \
  --file-system-id "$file_system_id" \
  --query 'FileSystems[0].LifeCycleState' \
  --output text)"
if [[ "$efs_state" != "available" ]]; then
  echo "EFS file system $file_system_id is not available (state: $efs_state)." >&2
  exit 1
fi

echo "Checking Docker Compose and the mounted EFS filesystem on Moodle nodes..."
ansible -i "$inventory_path" moodle -b -m shell -a '
  set -euo pipefail
  docker compose version
  findmnt --noheadings --output FSTYPE,TARGET --target /mnt/efs/moodledata
  test -d /opt/moodle/release
  test -d /opt/moodle/secrets
' >/dev/null

echo "Checking that each Moodle node can read the immutable image reference..."
if ! ansible -i "$inventory_path" moodle -b -m command -a "docker manifest inspect $image_ref" >/dev/null; then
  cat >&2 <<EOF
One or more Moodle nodes cannot read $image_ref from GHCR.
Run automation/configure-moodle-ghcr-access.sh with a PAT classic limited to
read:packages, then run this preflight again. This preflight made no changes.
EOF
  exit 1
fi

echo "Moodle preflight passed. RDS, EFS, host runtime, and GHCR image access are ready."
echo "ALB target health is expected to remain unhealthy until moodle-web is started."
aws elbv2 describe-target-health \
  --profile "$aws_profile" \
  --region "$region" \
  --target-group-arn "$target_group_arn" \
  --query 'TargetHealthDescriptions[].{target:Target.Id,state:TargetHealth.State}' \
  --output table
