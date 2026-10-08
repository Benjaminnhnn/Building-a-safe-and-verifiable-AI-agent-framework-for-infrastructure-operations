#!/usr/bin/env bash
# Read-only, scenario-specific recovery probes for reviewed staging live cases.
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
  DB-02) alert_name=MoodleSyntheticTransactionFailed ;;
  DB-03) alert_name=MoodleNodeRdsTcpFailed ;;
  RES-01) alert_name=MoodleNodeCpuHigh ;;
  RES-02) alert_name=MoodleNodeMemoryPressure ;;
  RES-03) alert_name=MoodleScratchEnospc ;;
  NET-01) alert_name=MoodleSyntheticTransactionFailed ;;
  NET-02) alert_name=MoodleNodeRdsTcpFailed ;;
  NET-03) alert_name=MoodleNodeRdsTcpLatencyHigh ;;
  CON-01) alert_name=MoodleWebContainerMissing ;;
  CON-02) alert_name=MoodleApacheRouterMissing ;;
  CON-03) alert_name=MoodleWebContainerRestarting ;;
  SEC-02) alert_name=MoodleSyntheticTransactionFailed ;;
  SEC-03) alert_name=MoodleTrustedProxyConfigDrift ;;
  *)
    echo "Scenario must be one of the fourteen staging live cases: DB-01, DB-02, DB-03, RES-01, RES-02, RES-03, NET-01, NET-02, NET-03, CON-01, CON-02, CON-03, SEC-02, SEC-03." >&2
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
  if [[ "$scenario" == DB-02 ]]; then
    remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=ALERTS{alertname="MoodleSyntheticTransactionFailed",alertstate="firing",instance="monitor-ai-01"}'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length == 0'\'' >/dev/null'
    return
  fi
  if [[ "$scenario" == CON-02 || "$scenario" == CON-03 || "$scenario" == NET-02 || "$scenario" == NET-03 || "$scenario" == DB-03 || "$scenario" == SEC-03 || "$scenario" == RES-02 || "$scenario" == RES-03 ]]; then
    remote monitor-ai-01 "
      curl --fail --silent --get \
        --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\",instance=\"moodle-app-b\"}' \
        http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length == 0' >/dev/null
    "
    return
  fi
  remote monitor-ai-01 "
    curl --fail --silent --get \
      --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\"}' \
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
alert_resolution_description="$alert_name is not firing"
[[ "$scenario" != CON-02 && "$scenario" != CON-03 && "$scenario" != NET-02 && "$scenario" != NET-03 && "$scenario" != DB-03 && "$scenario" != SEC-03 && "$scenario" != RES-02 && "$scenario" != RES-03 ]] || alert_resolution_description="$alert_name is not firing on moodle-app-b"
[[ "$scenario" != DB-02 ]] || alert_resolution_description="$alert_name is not firing on monitor-ai-01"
record_check scenario_alert_resolved \
  "$alert_resolution_description" \
  "$(if check_scenario_alert_resolved; then printf passed; else printf failed; fi)"

case "$scenario" in
  DB-01)
    for host in moodle-app-a moodle-app-b; do
      run_check "${host}_database_tls" \
        "The Moodle application's current PostgreSQL session uses TLS on $host" \
        remote "$host" 'sudo docker exec release-moodle-web-1 php -r '\''define("CLI_SCRIPT", true); require "/var/www/html/config.php"; $ssl = $DB->get_field_sql("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"); exit(in_array($ssl, [true, 1, "1", "t"], true) ? 0 : 1);'\'''
    done
    ;;
  DB-02)
    run_check app_role_quota_and_tagged_sessions_restored \
      'Moodle app-role quota is back to unlimited, tagged fixture sessions are gone, and the ownership fixture is absent' \
      bash -c 'test "$(bash "$1" status)" = "role_limit=-1 tagged_sessions=0 fixture=absent"' _ "$script_dir/moodle-db02-role-quota.sh"
    run_check independent_monitor_rds_probe_healthy \
      'The independent monitor-to-RDS TCP probe remains healthy' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=probe_success{job="blackbox_moodle_rds"} == 1'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
    ;;
  DB-03)
    run_check db03_backup_removed \
      'The app-b DB-03 runtime environment backup fixture is absent' \
      remote moodle-app-b 'sudo test ! -e /var/lib/moodle-faults/DB-03.env'
    run_check app_b_approved_database_endpoint \
      'The app-b web container uses the approved RDS hostname' \
      remote moodle-app-b "sudo docker exec release-moodle-web-1 sh -c 'test \"\$MOODLE_DB_HOST\" = \"$database_endpoint\"'"
    run_check app_b_database_tls \
      'The app-b Moodle application connects to PostgreSQL using TLS' \
      remote moodle-app-b 'sudo docker exec release-moodle-web-1 php -r '\''define("CLI_SCRIPT", true); require "/var/www/html/config.php"; $ssl = $DB->get_field_sql("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"); exit(in_array($ssl, [true, 1, "1", "t"], true) ? 0 : 1);'\'''
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
    ;;
  RES-01)
    run_check named_cpu_fixture_absent \
      'The controlled CPU-load container is absent from moodle-app-b' \
      remote moodle-app-b 'sudo docker info >/dev/null && ! sudo docker ps -a --format "{{.Names}}" | grep -Fxq moodle-fault-res-01'
    ;;
  RES-02)
    run_check named_memory_fixture_absent \
      'The controlled memory-load container and ownership fixture are absent from moodle-app-b' \
      remote moodle-app-b 'sudo test ! -e /var/lib/moodle-faults/RES-02.memory && ! sudo docker ps -a --format "{{.Names}}" | grep -Fxq moodle-fault-res-02'
    run_check memory_fixture_metric_absent \
      'Prometheus has no recent observation of the named memory fixture' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=container_memory_working_set_bytes{job="moodle_cadvisor",instance="moodle-app-b",name="moodle-fault-res-02"} and on(instance,name) (time() - container_last_seen{job="moodle_cadvisor",instance="moodle-app-b",name="moodle-fault-res-02"} < 30)'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length == 0'\'' >/dev/null'
    run_check app_b_web_container_healthy \
      'The app-b Moodle web container remained running and healthy' \
      remote moodle-app-b 'test "$(sudo docker inspect --format "{{.State.Running}} {{.State.Health.Status}}" release-moodle-web-1)" = "true healthy"'
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
    ;;
  RES-03)
    run_check scratch_mount_absent \
      'The isolated RES-03 scratch mountpoint is absent from moodle-app-b' \
      remote moodle-app-b 'sudo test ! -e /var/lib/moodle-faults/res03-scratch'
    run_check scratch_enospc_metric_cleared \
      'Node Exporter reports no isolated scratch ENOSPC fault' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_scratch_enospc{job="moodle_node",instance="moodle-app-b"} == 0'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'
    run_check app_b_efs_remains_mounted \
      'The Moodle EFS mount remains available on moodle-app-b' \
      remote moodle-app-b 'test "$(findmnt --noheadings --output FSTYPE --target /mnt/efs/moodledata)" = nfs4'
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
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
  NET-02)
    run_check tagged_port_block_absent \
      'The exact NET-02 DOCKER-USER fixture is absent from app-b' \
      remote moodle-app-b 'rules=$(sudo iptables -S DOCKER-USER) || exit 1; ! printf "%s\n" "$rules" | grep -Fq -- "--comment moodle-fault-NET-02"'
    for host in moodle-app-a moodle-app-b; do
      run_check "${host}_rds_tcp_healthy" \
        "The Moodle web container on $host can reach RDS TCP 5432" \
        remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=moodle_node_rds_tcp_success{job=\"moodle_node\",instance=\"$host\"} == 1' http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length > 0' >/dev/null"
    done
    run_check monitor_rds_probe_healthy \
      'The independent monitor-to-RDS TCP probe remains healthy' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=probe_success{job="blackbox_moodle_rds"} == 1'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
    ;;
  NET-03)
    run_check net03_fixture_removed \
      'The app-b NET-03 namespace ownership fixture is absent' \
      remote moodle-app-b 'sudo test ! -e /var/lib/moodle-faults/NET-03.netem'
    run_check net03_qdisc_restored \
      'The app-b Moodle web-container network namespace has its original noqueue qdisc' \
      remote moodle-app-b 'pid=$(sudo docker inspect --format "{{.State.Pid}}" release-moodle-web-1); sudo nsenter -t "$pid" -n tc qdisc show dev eth0 | grep -Eq "^qdisc noqueue 0: root"'
    for host in moodle-app-a moodle-app-b; do
      run_check "${host}_rds_tcp_latency_normal" \
        "The Moodle web container on $host has normal RDS TCP latency" \
        remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=moodle_node_rds_tcp_success{job=\"moodle_node\",instance=\"$host\"} == 1 and on() (moodle_node_rds_tcp_duration_seconds{job=\"moodle_node\",instance=\"$host\"} < 0.2)' http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length > 0' >/dev/null"
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
  CON-02)
    run_check moodle_app_b_router_enabled \
      'The approved Apache Moodle router configuration is enabled on moodle-app-b' \
      remote moodle-app-b 'sudo docker exec release-moodle-web-1 a2query -c moodle-router >/dev/null'
    run_check moodle_app_b_router_fallback_healthy \
      'Node Exporter reports the app-b Moodle router fallback healthy' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_router_fallback_success{job="moodle_node",instance="moodle-app-b"} == 1'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
    ;;
  CON-03)
    run_check con03_backup_removed \
      'The app-b CON-03 runtime environment backup fixture is absent' \
      remote moodle-app-b 'sudo test ! -e /var/lib/moodle-faults/CON-03.env'
    run_check app_b_wwwroot_approved \
      'The app-b web container uses the approved Moodle public URL' \
      remote moodle-app-b "sudo docker exec release-moodle-web-1 sh -c 'test \"\$MOODLE_WWWROOT\" = \"$public_url\"'"
    run_check app_b_web_container_healthy \
      'The app-b Moodle web container is running and healthy after recreation' \
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
  SEC-03)
    run_check sec03_backup_removed \
      'The app-b SEC-03 runtime environment backup fixture is absent' \
      remote moodle-app-b 'sudo test ! -e /var/lib/moodle-faults/SEC-03.env'
    run_check app_b_reverse_proxy_setting_approved \
      'The app-b web container has the approved reverse-proxy trust setting' \
      remote moodle-app-b 'sudo docker exec release-moodle-web-1 sh -c '\''test "$MOODLE_REVERSE_PROXY" = false'\'''
    run_check app_b_reverse_proxy_metric_approved \
      'Node Exporter reports the app-b reverse-proxy trust setting approved' \
      remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_reverse_proxy_enabled{job="moodle_node",instance="moodle-app-b"} == 0'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'
    run_check both_alb_targets_healthy \
      'Both Moodle ALB targets are healthy' check_two_healthy_alb_targets
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
