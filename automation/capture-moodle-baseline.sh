#!/usr/bin/env bash
# Capture a secret-free operational baseline for the deployed Moodle stack.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
artifacts_dir="$terraform_dir/.artifacts"
baseline_dir="$artifacts_dir/moodle-baseline"
ssh_config="$artifacts_dir/moodle-ssh.config"
aws_profile="${AWS_PROFILE:-target-account}"

for required_command in aws curl jq ssh terraform; do
  command -v "$required_command" >/dev/null 2>&1 || {
    echo "Required command not found: $required_command" >&2
    exit 127
  }
done

umask 077
mkdir -p "$baseline_dir"
terraform -chdir="$terraform_dir" output -raw moodle_ssh_config > "$ssh_config"
chmod 0600 "$ssh_config"

application_json="$(terraform -chdir="$terraform_dir" output -json moodle_application)"
database_json="$(terraform -chdir="$terraform_dir" output -json moodle_database)"
filesystem_json="$(terraform -chdir="$terraform_dir" output -json moodle_filesystem)"

region="$(jq -r '.alb_arn | split(":")[3]' <<<"$application_json")"
public_url="$(jq -r '.url' <<<"$application_json")"
target_group_arn="$(jq -r '.target_group_arn' <<<"$application_json")"
db_identifier="$(jq -r '.instance_identifier' <<<"$database_json")"
efs_id="$(jq -r '.file_system_id' <<<"$filesystem_json")"
captured_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

jq -n --arg captured_at "$captured_at" --arg region "$region" \
  '{captured_at: $captured_at, region: $region, system: "moodle-staging"}' \
  > "$baseline_dir/metadata.json"

aws elbv2 describe-target-health \
  --profile "$aws_profile" --region "$region" \
  --target-group-arn "$target_group_arn" \
  > "$baseline_dir/alb-target-health.json"

aws rds describe-db-instances \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$db_identifier" \
  > "$baseline_dir/rds-status.json"

aws rds describe-db-snapshots \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$db_identifier" \
  --snapshot-type manual \
  > "$baseline_dir/rds-snapshots.json"

aws efs describe-file-systems \
  --profile "$aws_profile" --region "$region" \
  --file-system-id "$efs_id" \
  > "$baseline_dir/efs-status.json"

aws efs describe-backup-policy \
  --profile "$aws_profile" --region "$region" \
  --file-system-id "$efs_id" \
  > "$baseline_dir/efs-backup-policy.json"

for host_name in moodle-app-a moodle-app-b; do
  ssh -F "$ssh_config" "$host_name" '
    set -eu
    hostname
    findmnt --target /mnt/efs/moodledata
    sudo docker ps --format "{{.Names}} | {{.Image}} | {{.Status}}" | sort
    curl --fail --silent --show-error http://127.0.0.1:8080/healthz.php
    echo
  ' > "$baseline_dir/${host_name}-runtime.txt"
done

ssh -F "$ssh_config" monitor-ai-01 \
  'curl --fail --silent http://127.0.0.1:9090/api/v1/targets' \
  > "$baseline_dir/prometheus-targets.json"

ssh -F "$ssh_config" monitor-ai-01 \
  'curl --fail --silent http://127.0.0.1:9090/api/v1/alerts' \
  > "$baseline_dir/prometheus-alerts.json"

ssh -F "$ssh_config" monitor-ai-01 '
  set -eu
  grafana_password="$(sudo cat /opt/moodle-observability/secrets/grafana-admin-password)"
  grafana_auth="admin:$grafana_password"
  health="$(curl --fail --silent http://127.0.0.1:3000/api/health)"
  datasource="$(curl --fail --silent -u "$grafana_auth" http://127.0.0.1:3000/api/datasources/uid/prometheus/health)"
  dashboard="$(curl --fail --silent -u "$grafana_auth" http://127.0.0.1:3000/api/dashboards/uid/moodle-infrastructure)"
  jq -n \
    --argjson health "$health" \
    --argjson datasource "$datasource" \
    --argjson dashboard "$dashboard" \
    "{health: {database: \$health.database, version: \$health.version}, datasource: {status: \$datasource.status, message: \$datasource.message}, dashboard: {uid: \$dashboard.dashboard.uid, title: \$dashboard.dashboard.title, panels: (\$dashboard.dashboard.panels | length), folder: \$dashboard.meta.folderTitle}}"
' > "$baseline_dir/grafana-status.json"

health_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' "$public_url/healthz.php")"
login_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' "$public_url/login/index.php")"
jq -n \
  --arg url "$public_url" \
  --arg health_code "$health_code" \
  --arg login_code "$login_code" \
  '{url: $url, health_status: ($health_code | tonumber), login_status: ($login_code | tonumber)}' \
  > "$baseline_dir/public-endpoints.json"

for json_file in "$baseline_dir"/*.json; do
  jq empty "$json_file"
done

echo "Moodle baseline captured at $baseline_dir"
