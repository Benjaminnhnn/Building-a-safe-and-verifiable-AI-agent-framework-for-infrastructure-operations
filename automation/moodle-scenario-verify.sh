#!/usr/bin/env bash
# Read-only, scenario-specific recovery probes for the five S3.3 live cases.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/moodle-fault-common.sh"

scenario="${1:-}"
drill_id="${2:-}"
if [[ -n "$drill_id" && ! "$drill_id" =~ ^[A-Z0-9-]+$ ]]; then
  echo "Drill ID may contain only uppercase letters, digits, and hyphens." >&2
  exit 64
fi
case "$scenario" in
  DB-01) alert_name=MoodleSyntheticTransactionFailed ;;
  RES-01) alert_name=MoodleNodeCpuHigh ;;
  NET-01) alert_name=MoodleSyntheticTransactionFailed ;;
  CON-01) alert_name=MoodleWebContainerMissing ;;
  SEC-02) alert_name=MoodleSyntheticTransactionFailed ;;
  *)
    echo "Scenario must be one of the five staging live cases: DB-01, RES-01, NET-01, CON-01, SEC-02." >&2
    exit 64
    ;;
esac

load_moodle_environment_read_only() {
  require_commands
  [[ -r "$ssh_config" ]] || {
    echo "Missing SSH configuration: $ssh_config" >&2
    exit 66
  }
  local application_json database_json filesystem_json
  application_json="$(terraform -chdir="$terraform_dir" output -json moodle_application)"
  database_json="$(terraform -chdir="$terraform_dir" output -json moodle_database)"
  filesystem_json="$(terraform -chdir="$terraform_dir" output -json moodle_filesystem)"
  public_url="$(jq -r '.url' <<<"$application_json")"
  database_endpoint="$(jq -r '.endpoint' <<<"$database_json")"
  target_group_arn="$(jq -r '.target_group_arn' <<<"$application_json")"
  region="$(jq -r '.alb_arn | split(":")[3]' <<<"$application_json")"
  moodledata_path="$(jq -r '.host_mount_path' <<<"$filesystem_json")"
  [[ "$public_url" == http://moodle-staging-* || "$public_url" == https://moodle-staging-* ]] || {
    echo "Refusing verification outside the Moodle staging URL: $public_url" >&2
    exit 77
  }
  [[ "$(jq -r '.legacy_demo_enabled' <<<"$application_json")" == false ]] || {
    echo "Refusing verification while the legacy demo is enabled." >&2
    exit 77
  }
}

load_moodle_environment_read_only
checks='[]'

record_check() {
  local check_id="$1" description="$2" status="$3"
  checks="$(jq -c --arg id "$check_id" --arg description "$description" --arg status "$status" \
    '. + [{id:$id,description:$description,status:$status}]' <<<"$checks")"
}

run_check() {
  local check_id="$1" description="$2" status=failed
  shift 2
  if "$@" >/dev/null 2>&1; then status=passed; fi
  record_check "$check_id" "$description" "$status"
}

check_prometheus_synthetic() {
  remote monitor-ai-01 "
    curl --fail --silent --get \
      --data-urlencode 'query=moodle_synthetic_success{job=\"moodle_synthetic\",instance=\"monitor-ai-01\"} == 1 and on() (time() - moodle_synthetic_last_run_timestamp_seconds{job=\"moodle_synthetic\",instance=\"monitor-ai-01\"} < 180)' \
      http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length > 0' >/dev/null
  "
}

check_scenario_alert_resolved() {
  remote monitor-ai-01 "
    curl --fail --silent --get \
      --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\",scenario_id=\"$scenario\"}' \
      http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length == 0' >/dev/null
  "
}

check_two_healthy_alb_targets() {
  local healthy
  healthy="$(aws elbv2 describe-target-health \
    --profile "${AWS_PROFILE:-target-account}" --region "$region" \
    --target-group-arn "$target_group_arn" \
    --query 'length(TargetHealthDescriptions[?TargetHealth.State==`healthy`])' --output text)"
  [[ "$healthy" == 2 ]]
}

record_check common_synthetic_transaction \
  'Prometheus has a successful authenticated Moodle transaction newer than 180 seconds' \
  "$(if check_prometheus_synthetic; then printf passed; else printf failed; fi)"
record_check scenario_alert_resolved \
  "$alert_name is not firing with scenario_id=$scenario" \
  "$(if check_scenario_alert_resolved; then printf passed; else printf failed; fi)"

case "$scenario" in
  DB-01)
    for host in moodle-app-a moodle-app-b; do
      run_check "${host}_database_tls" \
        "The Moodle application's current PostgreSQL session uses TLS on $host" \
        remote "$host" 'sudo docker exec release-moodle-web-1 php -r '\''define("CLI_SCRIPT", true); require "/var/www/html/config.php"; $ssl = $DB->get_field_sql("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"); exit(in_array($ssl, [true, 1, "1", "t"], true) ? 0 : 1);'\'''
    done
    ;;
  RES-01)
    run_check named_cpu_fixture_absent \
      'The controlled CPU-load container is absent from moodle-app-b' \
      remote moodle-app-b 'sudo docker info >/dev/null && ! sudo docker ps -a --format "{{.Names}}" | grep -Fxq moodle-fault-res-01'
    ;;
  NET-01)
    for host in moodle-app-a moodle-app-b; do
      run_check "${host}_database_dns" \
        "The configured RDS hostname resolves on $host" \
        remote "$host" "getent ahostsv4 '$database_endpoint'"
    done
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
    ;;
  CON-01)
    run_check moodle_app_b_container_healthy \
      'release-moodle-web-1 is running and healthy on moodle-app-b' \
      remote moodle-app-b 'test "$(sudo docker inspect --format "{{.State.Running}} {{.State.Health.Status}}" release-moodle-web-1)" = "true healthy"'
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
    ;;
  SEC-02)
    run_check fixture_directory_mode_restored \
      'The shared synthetic-fixtures directory has its reviewed 0770 mode' \
      remote moodle-app-a "test \"\$(sudo stat --format=%a '$moodledata_path/synthetic-fixtures')\" = 770"
    run_check efs_mount_metric_is_one \
      'Prometheus reports the Moodle EFS mount available on moodle-app-a' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_efs_mount_available{job="moodle_node",instance="moodle-app-a"} == 1'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'
    ;;
esac

status=passed
if jq -e 'all(.[]; .status == "passed")' <<<"$checks" >/dev/null; then
  status=passed
else
  status=failed
fi
jq -cn --arg scenario_id "$scenario" --arg drill_id "$drill_id" --arg status "$status" --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson checks "$checks" \
  '{scenario_id:$scenario_id,drill_id:(if $drill_id == "" then null else $drill_id end),status:$status,mode:"live_read_only",checked_at:$checked_at,checks:$checks}'
[[ "$status" == passed ]]
