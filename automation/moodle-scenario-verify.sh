#!/usr/bin/env bash
# Read-only, scenario-specific recovery probes for reviewed staging live cases.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/moodle-fault-common.sh"

scenario="${1:-}"
drill_id="${2:-}"
if [[ -n "$drill_id" && ! "$drill_id" =~ ^[A-Za-z0-9-]+$ ]]; then
  echo "Drill ID may contain only letters, digits, and hyphens." >&2
  exit 64
fi
case "$scenario" in
  DB-01) alert_name=MoodleSyntheticTransactionFailed ;;
  RES-01) alert_name=MoodleNodeCpuHigh ;;
  NET-01) alert_name=MoodleSyntheticTransactionFailed ;;
  CON-01) alert_name=MoodleWebContainerMissing ;;
  BIZ-01) alert_name=MoodleMaintenanceModeStuck ;;
  BIZ-02) alert_name=MoodleAdhocTaskQueueStuck ;;
  *)
    echo "Scenario must be one of the six agent-executable thesis scenarios: BIZ-01, BIZ-02, CON-01, DB-01, RES-01, NET-01 (SEC-01 is human-only)." >&2
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
  external_rds_health_check_id="$(jq -r '.external_tcp_health_check_id // empty' <<<"$database_json")"
  # Expected ALB targets come from the same Terraform state as the target group,
  # so the check follows the topology instead of assuming two nodes.
  expected_target_ids="$(jq -c '[(.nodes // {})[] | .id] | unique' <<<"$application_json")"
  validate_staging_application "$application_json" || exit 77
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
      --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\"}' \
      http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length == 0' >/dev/null
  "
}

# Passes only when the registered targets are exactly the expected Terraform
# nodes and every one is healthy. A fixed count (the old "== 2") passed with a
# third node down and failed forever with three healthy nodes; an empty
# expectation or an unregistered/extra target now fails closed.
check_all_expected_alb_targets_healthy() {
  local health
  health="$(aws elbv2 describe-target-health     --profile "${AWS_PROFILE:-target-account}" --region "$region"     --target-group-arn "$target_group_arn" --output json)" || return 1
  jq -e --argjson expected "${expected_target_ids:-[]}" '
    ($expected | length) > 0
    and ([.TargetHealthDescriptions[].Target.Id] | unique) == ($expected | unique)
    and all(.TargetHealthDescriptions[]; .TargetHealth.State == "healthy")
  ' <<<"$health" >/dev/null
}

check_internet_to_postgresql_blocked() {
  [[ -n "$external_rds_health_check_id" ]] || return 1
  local observations
  observations="$(AWS_PROFILE="${AWS_PROFILE:-target-account}" aws route53 get-health-check-status \
    --health-check-id "$external_rds_health_check_id" --output json)" || return 1
  # Route 53 reports CheckedTime as e.g. 2026-10-21T14:29:56.998000+00:00, but
  # jq's fromdateiso8601 accepts only YYYY-MM-DDTHH:MM:SSZ. Drop fractional
  # seconds and map the UTC offset to Z; any other offset still fails to parse,
  # which keeps the check fail-closed.
  jq -e '
    def checked_epoch: .StatusReport.CheckedTime
      | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601;
    .HealthCheckObservations as $observations
    | ($observations | length >= 3)
      and (($observations | map(.IPAddress) | unique | length) >= 3)
      and all($observations[];
        (.StatusReport.Status | startswith("Failure"))
        and ((now - checked_epoch) >= 0)
        and ((now - checked_epoch) <= 180)
      )
  ' <<<"$observations" >/dev/null
}

record_check common_synthetic_transaction \
  'Prometheus has a successful authenticated Moodle transaction newer than 180 seconds' \
  "$(if check_prometheus_synthetic; then printf passed; else printf failed; fi)"
alert_resolution_description="$alert_name is not firing"
record_check scenario_alert_resolved \
  "$alert_resolution_description" \
  "$(if check_scenario_alert_resolved; then printf passed; else printf failed; fi)"
record_check internet_to_postgresql_blocked \
  'At least three distinct external Route 53 checkers recently failed to connect to PostgreSQL TCP 5432' \
  "$(if check_internet_to_postgresql_blocked; then printf passed; else printf failed; fi)"

case "$scenario" in
  DB-01)
    for host in moodle-app-a moodle-app-b; do
      run_check "${host}_database_tls" \
        "The Moodle application's current PostgreSQL session uses TLS on $host" \
        remote "$host" 'sudo docker exec release-moodle-web-1 php -r '\''define("CLI_SCRIPT", true); require "/var/www/html/config.php"; $ssl = $DB->get_field_sql("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"); exit(in_array($ssl, [true, 1, "1", "t"], true) ? 0 : 1);'\'''
    done
    run_check both_alb_targets_healthy \
      'Every expected Moodle ALB target (from Terraform nodes) is registered and healthy' check_all_expected_alb_targets_healthy
    ;;
  RES-01)
    run_check named_cpu_fixture_absent \
      'The controlled CPU-load container is absent from moodle-app-b' \
      remote moodle-app-b 'sudo docker info >/dev/null && ! sudo docker ps -a --format "{{.Names}}" | grep -Fxq moodle-fault-res-01'
    ;;
  NET-01)
    for host in moodle-app-a moodle-app-b; do
      run_check "${host}_database_dns" \
        "The configured RDS hostname resolves to a non-loopback address inside Moodle on $host" \
        remote "$host" "sudo docker exec release-moodle-web-1 getent ahostsv4 '$database_endpoint' | awk '\$1 !~ /^127\\./ && \$1 != \"0.0.0.0\" { found=1 } END { exit !found }'"
    done
    run_check both_alb_targets_healthy \
      'Every expected Moodle ALB target (from Terraform nodes) is registered and healthy' check_all_expected_alb_targets_healthy
    ;;
  CON-01)
    run_check moodle_app_b_container_healthy \
      'release-moodle-web-1 is running and healthy on moodle-app-b' \
      remote moodle-app-b 'test "$(sudo docker inspect --format "{{.State.Running}} {{.State.Health.Status}}" release-moodle-web-1)" = "true healthy"'
    run_check both_alb_targets_healthy \
      'Every expected Moodle ALB target (from Terraform nodes) is registered and healthy' check_all_expected_alb_targets_healthy
    ;;
  BIZ-01)
    run_check moodle_cli_maintenance_disabled \
      'The Moodle CLI maintenance mode flag file climaintenance.html is absent' \
      remote moodle-app-a "test ! -e '$moodledata_path/climaintenance.html'"
    # The login page returns 503 while CLI maintenance is on, unlike the
    # static healthz endpoint.
    run_check student_course_view_http_200 \
      'The Moodle login page returns HTTP 200 to a non-admin visitor' \
      curl --fail --silent -o /dev/null "$public_url/login/index.php"
    ;;
  BIZ-02)
    run_check moodle_cron_service_active \
      'The moodle-cron container on moodle-app-a is running and healthy' \
      remote moodle-app-a 'test "$(sudo docker inspect --format "{{.State.Running}}" release-moodle-cron-1 2>/dev/null)" = "true"'
    run_check overdue_adhoc_tasks_zero \
      'Prometheus reports overdue Moodle adhoc tasks count is zero' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '''query=moodle_adhoc_tasks_overdue_count{job="moodle_node"} == 0''' http://127.0.0.1:9090/api/v1/query | jq -e '''.data.result | length > 0''' >/dev/null'
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
