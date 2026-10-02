#!/usr/bin/env bash
# Scoped, fail-closed RDS administrator operations for the staging DB-02 drill.
# Never prints an RDS password or accepts arbitrary SQL/role names.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

action="${1:-}"
case "$action" in prepare|set-one|restore|status) ;; *) echo "Usage: $0 <prepare|set-one|restore|status>" >&2; exit 64 ;; esac
if [[ "$action" == prepare || "$action" == set-one ]]; then
  [[ "${MOODLE_FAULT_CONFIRM:-}" == staging ]] || { echo "Set MOODLE_FAULT_CONFIRM=staging" >&2; exit 77; }
fi
load_moodle_environment
for command_name in docker curl ss; do command -v "$command_name" >/dev/null || exit 127; done

profile="${AWS_PROFILE:-target-account}"
identity="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
[[ "$identity" == 583845872420 && "$region" == ap-southeast-1 ]] || { echo "Refusing non-staging AWS account or region" >&2; exit 77; }
db_identifier="$(jq -r '.instance_identifier' <<<"$database_json")"
db_name="$(jq -r '.database_name' <<<"$database_json")"
secret_arn="$(jq -r '.master_secret_arn' <<<"$database_json")"
[[ "$db_identifier" == moodle-staging-postgres && "$db_name" == moodle && "$secret_arn" == arn:aws:secretsmanager:ap-southeast-1:583845872420:secret:* ]] || exit 77
fixture="$fault_artifacts/DB-02.limit"
port=15434
if ss -ltn "sport = :$port" | grep -q LISTEN; then echo "Local RDS tunnel port is busy" >&2; exit 69; fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/moodle-db02.XXXXXX")"
ssh_pid=""
cleanup() {
  [[ -z "$ssh_pid" ]] || kill "$ssh_pid" 2>/dev/null || true
  unset master_password secret_json
  [[ -z "$work_dir" || ! -d "$work_dir" ]] || rm -r -- "$work_dir"
}
trap cleanup EXIT
chmod 0700 "$work_dir"

secret_json="$(aws secretsmanager get-secret-value --profile "$profile" --region "$region" --secret-id "$secret_arn" --query SecretString --output text)"
master_user="$(jq -r '.username' <<<"$secret_json")"
master_password="$(jq -r '.password' <<<"$secret_json")"
[[ "$master_user" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ && -n "$master_password" && "$master_password" != *$'\n'* ]] || exit 65
printf 'PGPASSWORD=%s\n' "$master_password" > "$work_dir/pg.env"
chmod 0600 "$work_dir/pg.env"
unset master_password secret_json
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem --output "$work_dir/rds-ca.pem"
grep -q -- '-----BEGIN CERTIFICATE-----' "$work_dir/rds-ca.pem"
chmod 0644 "$work_dir/rds-ca.pem"

ssh -F "$ssh_config" -N -L "127.0.0.1:$port:$database_endpoint:5432" moodle-app-a &
ssh_pid=$!
for _ in {1..30}; do
  if ss -ltn "sport = :$port" | grep -q LISTEN; then break; fi
  sleep 0.2
done
ss -ltn "sport = :$port" | grep -q LISTEN || { echo "RDS SSH tunnel failed" >&2; exit 69; }

query() {
  local sql="$1"
  docker run --rm --network host --env-file "$work_dir/pg.env" \
    -v "$work_dir/rds-ca.pem:/run/rds-ca.pem:ro" postgres:16.10-alpine \
    psql "host=$database_endpoint hostaddr=127.0.0.1 port=$port dbname=$db_name user=$master_user sslmode=verify-full sslrootcert=/run/rds-ca.pem connect_timeout=5" \
    -X -A -t -q -v ON_ERROR_STOP=1 -c "$sql"
}
role_limit() { query "SELECT rolconnlimit FROM pg_roles WHERE rolname='moodle_app'"; }
tagged_count() { query "SELECT count(*) FROM pg_stat_activity WHERE usename='moodle_app' AND application_name='moodle-fault-DB-02'"; }

case "$action" in
  prepare)
    [[ ! -e "$fixture" ]] || { echo "DB-02 ownership fixture already exists" >&2; exit 77; }
    [[ "$(aws rds describe-db-snapshots --profile "$profile" --region "$region" --db-snapshot-identifier moodle-sprint6-baseline-20261001 --query 'DBSnapshots[0].Status' --output text)" == available ]] || { echo "Pinned RDS snapshot is unavailable" >&2; exit 77; }
    [[ "$(tagged_count)" == 0 ]] || { echo "Preexisting DB-02 tagged sessions" >&2; exit 77; }
    old_limit="$(role_limit)"
    [[ "$old_limit" == -1 ]] || { echo "Expected the reviewed unlimited Moodle role quota (-1)" >&2; exit 77; }
    printf '%s\n' "$old_limit" > "$fixture"
    chmod 0600 "$fixture"
    echo "DB-02 prepared; previous Moodle role connection limit: $old_limit"
    ;;
  set-one)
    [[ -r "$fixture" ]] || { echo "DB-02 ownership fixture is missing" >&2; exit 77; }
    [[ "$(tagged_count)" -ge 2 ]] || { echo "Two tagged fixture sessions were not established" >&2; exit 77; }
    [[ "$(role_limit)" == "$(<"$fixture")" ]] || { echo "Moodle role quota changed unexpectedly" >&2; exit 77; }
    query 'ALTER ROLE moodle_app CONNECTION LIMIT 1' >/dev/null
    [[ "$(role_limit)" == 1 ]] || exit 1
    echo "DB-02 app-role quota set to one; no instance-wide parameter was changed"
    ;;
  restore)
    if [[ -r "$fixture" ]]; then
      old_limit="$(<"$fixture")"
      [[ "$old_limit" == -1 ]] || { echo "Malformed DB-02 prior quota" >&2; exit 77; }
      query "ALTER ROLE moodle_app CONNECTION LIMIT $old_limit" >/dev/null
      query "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='moodle_app' AND application_name='moodle-fault-DB-02'" >/dev/null
      [[ "$(role_limit)" == "$old_limit" && "$(tagged_count)" == 0 ]] || { echo "DB-02 restore did not verify" >&2; exit 1; }
      rm -f -- "$fixture"
      echo "DB-02 exact prior role quota restored; only tagged fixture sessions terminated"
    elif [[ "$(tagged_count)" != 0 || "$(role_limit)" != -1 ]]; then
      echo "DB-02 ownership fixture is absent but role/session state is not the reviewed baseline" >&2
      exit 77
    fi
    ;;
  status)
    printf 'role_limit=%s tagged_sessions=%s fixture=%s\n' "$(role_limit)" "$(tagged_count)" "$(if [[ -r "$fixture" ]]; then printf present; else printf absent; fi)"
    ;;
esac
