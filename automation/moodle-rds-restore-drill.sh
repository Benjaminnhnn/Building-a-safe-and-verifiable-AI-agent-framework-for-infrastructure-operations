#!/usr/bin/env bash
# Restore the latest manual Moodle RDS snapshot, verify read-only connectivity,
# record the result, and always remove the temporary database instance.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
artifacts_dir="$terraform_dir/.artifacts"
baseline_dir="$artifacts_dir/moodle-baseline"
ssh_config="$artifacts_dir/moodle-ssh.config"
aws_profile="${AWS_PROFILE:-target-account}"
snapshot_id="${1:-}"

for required_command in aws jq ssh terraform; do
  command -v "$required_command" >/dev/null 2>&1 || {
    echo "Required command not found: $required_command" >&2
    exit 127
  }
done

umask 077
mkdir -p "$baseline_dir"
terraform -chdir="$terraform_dir" output -raw moodle_ssh_config > "$ssh_config"
chmod 0600 "$ssh_config"

database_json="$(terraform -chdir="$terraform_dir" output -json moodle_database)"
application_json="$(terraform -chdir="$terraform_dir" output -json moodle_application)"
source_identifier="$(jq -r '.instance_identifier' <<<"$database_json")"
region="$(jq -r '.alb_arn | split(":")[3]' <<<"$application_json")"

if [[ -z "$snapshot_id" ]]; then
  snapshot_id="$(aws rds describe-db-snapshots \
    --profile "$aws_profile" --region "$region" \
    --db-instance-identifier "$source_identifier" \
    --snapshot-type manual \
    --query 'reverse(sort_by(DBSnapshots,&SnapshotCreateTime))[0].DBSnapshotIdentifier' \
    --output text)"
fi

[[ "$snapshot_id" != "None" && -n "$snapshot_id" ]] || {
  echo "No manual RDS snapshot is available for $source_identifier." >&2
  exit 66
}

source_db="$(aws rds describe-db-instances \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$source_identifier" \
  --query 'DBInstances[0]' --output json)"

db_class="${MOODLE_RESTORE_DB_INSTANCE_CLASS:-$(jq -r '.DBInstanceClass' <<<"$source_db")}"
subnet_group="$(jq -r '.DBSubnetGroup.DBSubnetGroupName' <<<"$source_db")"
parameter_group="$(jq -r '.DBParameterGroups[0].DBParameterGroupName' <<<"$source_db")"
mapfile -t security_groups < <(jq -r '.VpcSecurityGroups[].VpcSecurityGroupId' <<<"$source_db")

restore_id="moodle-staging-restore-drill-$(date -u +%Y%m%d%H%M%S)"
[[ "$restore_id" =~ ^moodle-staging-restore-drill-[0-9]{14}$ ]] || exit 64
created=false

cleanup() {
  if [[ "$created" == true ]]; then
    echo "Cleaning up temporary RDS instance $restore_id..." >&2
    aws rds delete-db-instance \
      --profile "$aws_profile" --region "$region" \
      --db-instance-identifier "$restore_id" \
      --skip-final-snapshot --delete-automated-backups >/dev/null 2>&1 || true
    aws rds wait db-instance-deleted \
      --profile "$aws_profile" --region "$region" \
      --db-instance-identifier "$restore_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Restoring $snapshot_id as temporary instance $restore_id..."
aws rds restore-db-instance-from-db-snapshot \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$restore_id" \
  --db-snapshot-identifier "$snapshot_id" \
  --db-instance-class "$db_class" \
  --db-subnet-group-name "$subnet_group" \
  --db-parameter-group-name "$parameter_group" \
  --vpc-security-group-ids "${security_groups[@]}" \
  --no-publicly-accessible \
  --no-multi-az \
  --no-auto-minor-version-upgrade \
  --tags Key=Purpose,Value=MoodleRestoreDrill Key=Environment,Value=staging \
  >/dev/null
created=true

aws rds wait db-instance-available \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$restore_id"

restore_endpoint="$(aws rds describe-db-instances \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$restore_id" \
  --query 'DBInstances[0].Endpoint.Address' --output text)"

connection_result="$(ssh -F "$ssh_config" moodle-app-a \
  "sudo docker exec -i -e MOODLE_RESTORE_HOST='$restore_endpoint' release-moodle-web-1 php" <<'PHP'
<?php
$password = rtrim(file_get_contents(getenv('MOODLE_DB_PASSWORD_FILE')), "\r\n");
putenv('PGPASSWORD=' . $password);
$connection = pg_connect(
    'host=' . getenv('MOODLE_RESTORE_HOST') .
    ' port=5432 dbname=moodle user=moodle_app' .
    ' sslmode=verify-full sslrootcert=/run/moodle-secrets/rds-ca.pem connect_timeout=10'
);
if (!$connection) {
    fwrite(STDERR, "restore_connection_failed\n");
    exit(1);
}
$result = pg_query($connection, 'SELECT current_database(), current_user');
$row = pg_fetch_row($result);
echo $row[0] . '|' . $row[1] . PHP_EOL;
PHP
)"

[[ "$connection_result" == "moodle|moodle_app" ]] || {
  echo "Unexpected restore connectivity result." >&2
  exit 1
}

echo "Restore connectivity passed; deleting temporary RDS instance..."
aws rds delete-db-instance \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$restore_id" \
  --skip-final-snapshot --delete-automated-backups >/dev/null
aws rds wait db-instance-deleted \
  --profile "$aws_profile" --region "$region" \
  --db-instance-identifier "$restore_id"
created=false
completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

jq -n \
  --arg started_at "$started_at" \
  --arg completed_at "$completed_at" \
  --arg source_snapshot "$snapshot_id" \
  --arg temporary_instance "$restore_id" \
  --arg db_class "$db_class" \
  '{status: "passed", started_at: $started_at, completed_at: $completed_at, source_snapshot: $source_snapshot, temporary_instance: $temporary_instance, instance_class: $db_class, connectivity: {database: "moodle", user: "moodle_app", tls: "verify-full", result: "passed"}, cleanup: "deleted"}' \
  > "$baseline_dir/rds-restore-drill.json"

trap - EXIT
echo "RDS restore drill passed and evidence was written to $baseline_dir/rds-restore-drill.json"
