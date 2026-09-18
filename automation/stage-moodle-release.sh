#!/usr/bin/env bash
# Stage, but do not deploy, the Moodle release on both application nodes.
# This script creates the least-privilege RDS role and runtime files readable
# only by the Moodle container's dedicated numeric group.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
ssh_config_path="$terraform_dir/.artifacts/moodle-ssh.config"
compose_source="$repo_root/release/moodle/docker-compose.yml"
aws_profile="${AWS_PROFILE:-target-account}"
image_ref="${1:-}"
db_password_file="${2:-}"
admin_password_file="${3:-}"
app_user="moodle_app"
# This numeric group is used only by the Moodle containers.  It must not be a
# login group on the EC2 hosts; files remain inaccessible to the SSH user.
moodle_secret_gid="1999"
tunnel_port="${MOODLE_RDS_TUNNEL_PORT:-15432}"
ca_bundle_url="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
hosts=(moodle-app-a moodle-app-b)

usage() {
  cat >&2 <<EOF
Usage: $0 <immutable-ghcr-image> <moodle-db-password-file> <moodle-admin-password-file>

This stages the RDS application role, CA bundle, secrets, Compose file, and
runtime environment. It does not start a Moodle container.
EOF
  exit 64
}

[[ -n "$image_ref" && -n "$db_password_file" && -n "$admin_password_file" ]] || usage
[[ "$image_ref" =~ ^ghcr\.io/[a-z0-9-]+/moodle:[a-f0-9]{40}$ ]] || {
  echo "Image must be a GHCR Moodle image tagged by a 40-character commit SHA." >&2
  exit 64
}
[[ "$tunnel_port" =~ ^[1-9][0-9]{3,4}$ ]] || {
  echo "MOODLE_RDS_TUNNEL_PORT must be a TCP port number." >&2
  exit 64
}

for required_command in aws curl docker jq openssl ss ssh terraform; do
  command -v "$required_command" >/dev/null 2>&1 || {
    echo "Required command not found: $required_command" >&2
    exit 127
  }
done

for required_file in "$ssh_config_path" "$compose_source" "$db_password_file" "$admin_password_file"; do
  [[ -f "$required_file" && -r "$required_file" ]] || {
    echo "Required readable file is missing: $required_file" >&2
    exit 66
  }
done

secret_file_is_private() {
  local path="$1" mode
  mode="$(stat -c '%a' "$path")"
  if (( (8#$mode & 0077) != 0 )); then
    echo "Secret file must not be readable by group/other: $path (mode $mode)" >&2
    exit 77
  fi
}

secret_file_is_private "$db_password_file"
secret_file_is_private "$admin_password_file"
[[ -s "$db_password_file" && -s "$admin_password_file" ]] || {
  echo "Secret files must not be empty." >&2
  exit 64
}

application_json="$(terraform -chdir="$terraform_dir" output -json moodle_application)"
database_json="$(terraform -chdir="$terraform_dir" output -json moodle_database)"
filesystem_json="$(terraform -chdir="$terraform_dir" output -json moodle_filesystem)"

region="$(jq -r '.alb_arn | split(":")[3]' <<<"$application_json")"
app_url="$(jq -r '.url' <<<"$application_json")"
app_scheme="$(jq -r '.scheme' <<<"$application_json")"
db_endpoint="$(jq -r '.endpoint' <<<"$database_json")"
db_port="$(jq -r '.port' <<<"$database_json")"
db_name="$(jq -r '.database_name' <<<"$database_json")"
master_secret_arn="$(jq -r '.master_secret_arn' <<<"$database_json")"
data_mount_path="$(jq -r '.host_mount_path' <<<"$filesystem_json")"

[[ "$master_secret_arn" != "null" && -n "$master_secret_arn" ]] || {
  echo "Terraform state does not expose an RDS master secret ARN." >&2
  exit 65
}

case "$app_scheme" in
  http) moodle_ssl_proxy=false ;;
  https) moodle_ssl_proxy=true ;;
  *)
    echo "Unexpected Moodle application scheme from Terraform: $app_scheme" >&2
    exit 65
    ;;
esac

aws sts get-caller-identity --profile "$aws_profile" --output json >/dev/null
master_secret_json="$(aws secretsmanager get-secret-value \
  --profile "$aws_profile" \
  --region "$region" \
  --secret-id "$master_secret_arn" \
  --query SecretString \
  --output text)"
master_user="$(jq -r '.username' <<<"$master_secret_json")"
master_password="$(jq -r '.password' <<<"$master_secret_json")"
[[ -n "$master_user" && "$master_user" != "null" && -n "$master_password" && "$master_password" != "null" ]] || {
  echo "The RDS master secret is missing username or password." >&2
  exit 65
}

if ss -ltn "sport = :$tunnel_port" | grep -q LISTEN; then
  echo "Local tunnel port $tunnel_port is already in use. Set MOODLE_RDS_TUNNEL_PORT." >&2
  exit 69
fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/moodle-stage.XXXXXX")"
ssh_pid=""
cleanup() {
  [[ -n "$ssh_pid" ]] && kill "$ssh_pid" 2>/dev/null || true
  unset master_password
  rm -rf "$work_dir"
}
trap cleanup EXIT
chmod 0700 "$work_dir"

ca_bundle_path="$work_dir/rds-ca.pem"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "$ca_bundle_url" --output "$ca_bundle_path"
grep -q -- '-----BEGIN CERTIFICATE-----' "$ca_bundle_path" || {
  echo "Downloaded RDS CA bundle is not PEM data." >&2
  exit 65
}
chmod 0600 "$ca_bundle_path"

master_pgpass="$work_dir/master.pgpass"
printf '%s:%s:%s:%s:%s\n' "$db_endpoint" "$tunnel_port" "$db_name" "$master_user" "$master_password" > "$master_pgpass"
printf '%s:%s:%s:%s:%s\n' "127.0.0.1" "$tunnel_port" "$db_name" "$master_user" "$master_password" >> "$master_pgpass"
chmod 0600 "$master_pgpass"
unset master_password

cp "$db_password_file" "$work_dir/moodle-db-password"
cp "$admin_password_file" "$work_dir/moodle-admin-password"
chmod 0600 "$work_dir/moodle-db-password" "$work_dir/moodle-admin-password"

cat > "$work_dir/provision-role.sql" <<'SQL'
\set app_password `cat /run/moodle-stage/moodle-db-password`
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user');
\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password');
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'app_user');
\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'app_user');
\gexec
SQL
chmod 0600 "$work_dir/provision-role.sql"

echo "Opening a temporary SSH tunnel to provision the Moodle RDS role..."
ssh -F "$ssh_config_path" -N \
  -L "127.0.0.1:${tunnel_port}:${db_endpoint}:${db_port}" moodle-app-a &
ssh_pid=$!
for _ in {1..25}; do
  if ss -ltn "sport = :$tunnel_port" | grep -q LISTEN; then
    break
  fi
  sleep 0.2
done
ss -ltn "sport = :$tunnel_port" | grep -q LISTEN || {
  echo "SSH tunnel did not become ready." >&2
  exit 69
}

echo "Creating or rotating the least-privilege Moodle database role..."
docker run --rm --network host \
  -v "$work_dir:/run/moodle-stage:ro" \
  -e PGPASSFILE=/run/moodle-stage/master.pgpass \
  postgres:16.10-alpine \
  psql "host=${db_endpoint} hostaddr=127.0.0.1 port=${tunnel_port} dbname=${db_name} user=${master_user} sslmode=verify-full sslrootcert=/run/moodle-stage/rds-ca.pem" \
  -v ON_ERROR_STOP=1 \
  -v app_user="$app_user" \
  -v db_name="$db_name" \
  -f /run/moodle-stage/provision-role.sql >/dev/null

echo "Verifying the Moodle application role can connect with TLS..."
app_pgpass="$work_dir/app.pgpass"
app_password="$(tr -d '\r\n' < "$work_dir/moodle-db-password")"
printf '%s:%s:%s:%s:%s\n' "$db_endpoint" "$tunnel_port" "$db_name" "$app_user" "$app_password" > "$app_pgpass"
printf '%s:%s:%s:%s:%s\n' "127.0.0.1" "$tunnel_port" "$db_name" "$app_user" "$app_password" >> "$app_pgpass"
unset app_password
chmod 0600 "$app_pgpass"
docker run --rm --network host \
  -v "$work_dir:/run/moodle-stage:ro" \
  -e PGPASSFILE=/run/moodle-stage/app.pgpass \
  postgres:16.10-alpine \
  psql "host=${db_endpoint} hostaddr=127.0.0.1 port=${tunnel_port} dbname=${db_name} user=${app_user} sslmode=verify-full sslrootcert=/run/moodle-stage/rds-ca.pem" \
  -v ON_ERROR_STOP=1 -Atqc 'SELECT current_user' | grep -qx "$app_user"

runtime_env="$work_dir/moodle-runtime.env"
cat > "$runtime_env" <<EOF
MOODLE_IMAGE=$image_ref
MOODLE_DATA_DIR=$data_mount_path
MOODLE_DATA_ROOT=/var/moodledata
MOODLE_SECRETS_DIR=/opt/moodle/secrets
MOODLE_DB_HOST=$db_endpoint
MOODLE_DB_PORT=$db_port
MOODLE_DB_NAME=$db_name
MOODLE_DB_USER=$app_user
MOODLE_WWWROOT=$app_url
MOODLE_SSL_PROXY=$moodle_ssl_proxy
MOODLE_SITE_FULLNAME=Moodle Staging
MOODLE_SITE_SHORTNAME=Moodle
MOODLE_SITE_SUMMARY=Infrastructure operations test environment
MOODLE_ADMIN_USER=admin
MOODLE_ADMIN_EMAIL=admin@example.com
MOODLE_CRON_INTERVAL_SECONDS=60
EOF
chmod 0600 "$runtime_env"

copy_as_root() {
  local host="$1" source_path="$2" destination_path="$3" owner="$4" group="$5" mode="$6"
  cat "$source_path" | ssh -F "$ssh_config_path" "$host" \
    "sudo install -d -o root -g root -m 0750 '$(dirname "$destination_path")' && sudo tee '$destination_path' >/dev/null && sudo chown '$owner:$group' '$destination_path' && sudo chmod '$mode' '$destination_path'"
}

for host in "${hosts[@]}"; do
  echo "Staging Moodle release files on $host..."
  copy_as_root "$host" "$compose_source" /opt/moodle/release/docker-compose.yml root root 0640
  copy_as_root "$host" "$runtime_env" /opt/moodle/release/moodle-runtime.env root root 0640
  copy_as_root "$host" "$work_dir/moodle-db-password" /opt/moodle/secrets/moodle-db-password root "$moodle_secret_gid" 0640
  copy_as_root "$host" "$work_dir/moodle-admin-password" /opt/moodle/secrets/moodle-admin-password root "$moodle_secret_gid" 0640
  copy_as_root "$host" "$ca_bundle_path" /opt/moodle/secrets/rds-ca.pem root "$moodle_secret_gid" 0640
  ssh -F "$ssh_config_path" "$host" \
    "sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env -f /opt/moodle/release/docker-compose.yml config >/dev/null"
  ssh -F "$ssh_config_path" "$host" \
    "sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env -f /opt/moodle/release/docker-compose.yml --profile installer run --rm --no-deps --entrypoint /bin/sh moodle-install -c 'test -r /run/moodle-secrets/moodle-db-password && test -r /run/moodle-secrets/moodle-admin-password && test -r /run/moodle-secrets/rds-ca.pem' >/dev/null"
done

echo "Moodle release staging completed. No Moodle container has been started."
echo "Review the staged files and run the separately reviewed deployment commands next."
