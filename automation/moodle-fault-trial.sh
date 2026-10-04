#!/usr/bin/env bash
# Run one inject-observe-reset smoke trial and retain secret-free evidence.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
validate_scenario "$scenario"
load_moodle_environment
run_id="${scenario}-$(date -u +%Y%m%dT%H%M%SZ)"
result_file="$fault_artifacts/$run_id.json"
reset_needed=false

cleanup() {
  if [[ "$reset_needed" == true ]]; then
    "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null || true
  fi
}
trap cleanup EXIT

"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
reset_needed=true
MOODLE_FAULT_CONFIRM=staging "$script_dir/moodle-fault-inject.sh" "$scenario" >/dev/null
observed=false
observation=""
expected_alert=""
expected_instance=""
res02_memory_before_kib=""
res02_memory_during_kib=""
res02_memory_recovered_kib=""

case "$scenario" in
  DB-01|NET-01|SEC-02)
    if run_synthetic_once >/dev/null 2>&1; then
      observation="synthetic transaction unexpectedly passed"
    else
      observed=true
      observation="synthetic transaction failed as expected"
      expected_alert="MoodleSyntheticTransactionFailed"
    fi
    ;;
  DB-02)
    if [[ "$(bash "$script_dir/moodle-db02-role-quota.sh" status)" == "role_limit=1 tagged_sessions="*" fixture=present" ]]; then
      if run_synthetic_once >/dev/null 2>&1; then
        observation="synthetic transaction unexpectedly passed despite the app-role quota"
      else
        observed=true
        observation="Moodle app-role quota is one with tagged fixture sessions; authenticated synthetic failed while RDS TCP remained available"
        expected_alert="MoodleSyntheticTransactionFailed"
        expected_instance="monitor-ai-01"
      fi
    fi
    ;;
  DB-03)
    if remote moodle-app-b 'sudo grep -qx "MOODLE_DB_HOST=db03-invalid.invalid" /opt/moodle/release/moodle-runtime.env'; then
      for _ in {1..18}; do
        if remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_rds_tcp_success{job="moodle_node",instance="moodle-app-b"} == 0 and on() (moodle_node_rds_tcp_success{job="moodle_node",instance="moodle-app-a"} == 1) and on() (probe_success{job="blackbox_moodle_rds"} == 1)'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'; then
          observed=true
          observation="only app-b runtime DB hostname drifted and its RDS TCP probe failed while app-a and monitor stayed healthy"
          expected_alert="MoodleNodeRdsTcpFailed"
          expected_instance="moodle-app-b"
          break
        fi
        sleep 5
      done
    fi
    ;;
  RES-01)
    if remote moodle-app-b "test \"\$(sudo docker inspect --format '{{.State.Running}}' moodle-fault-res-01)\" = true"; then
      observed=true
      observation="bounded CPU load container is running"
      expected_alert="MoodleNodeCpuHigh"
    fi
    ;;
  RES-02)
    if remote moodle-app-b 'test "$(sudo docker inspect --format "{{.State.Running}}" moodle-fault-res-02)" = true'; then
      read -r res02_memory_before_kib res02_memory_during_kib < <(remote moodle-app-b 'sudo awk "NR==1 {before=\$1} NR==2 {printf \"%s %s\\n\", before, \$1}" /var/lib/moodle-faults/RES-02.memory')
      [[ "$res02_memory_before_kib" =~ ^[0-9]+$ && "$res02_memory_during_kib" =~ ^[0-9]+$ ]] || exit 1
      (( res02_memory_before_kib - res02_memory_during_kib >= 32768 )) || exit 1
      run_synthetic_once >/dev/null 2>&1 || { echo 'RES-02 synthetic transaction did not remain healthy' >&2; exit 1; }
      remote moodle-app-b 'test "$(sudo docker inspect --format "{{.State.OOMKilled}}" release-moodle-web-1)" = false' || exit 1
      for _ in {1..18}; do
        if remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=container_memory_working_set_bytes{job="moodle_cadvisor",instance="moodle-app-b",name="moodle-fault-res-02"} > 67108864'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'; then
          observed=true
          observation="app-b named, capped container allocated 96 MiB; host available memory dropped at least 32 MiB"
          expected_alert="MoodleNodeMemoryPressure"
          expected_instance="moodle-app-b"
          break
        fi
        sleep 5
      done
    fi
    ;;
  RES-03)
    for _ in {1..18}; do
      if remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_scratch_enospc{job="moodle_node",instance="moodle-app-b"} == 1 and on() (moodle_efs_mount_available{job="moodle_node",instance="moodle-app-b"} == 1)'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'; then
        observed=true
        observation="isolated app-b scratch tmpfs reached ENOSPC while EFS remained mounted"
        expected_alert="MoodleScratchEnospc"
        expected_instance="moodle-app-b"
        break
      fi
      sleep 5
    done
    ;;
  CON-01)
    for _ in {1..18}; do
      healthy_count="$(aws elbv2 describe-target-health --profile "${AWS_PROFILE:-target-account}" --region "$region" --target-group-arn "$target_group_arn" --query 'TargetHealthDescriptions[?TargetHealth.State==`healthy`]' --output json | jq length)"
      if [[ "$healthy_count" -eq 1 ]]; then observed=true; observation="one ALB target became unhealthy while one remained healthy"; break; fi
      sleep 5
    done
    expected_alert="MoodleWebContainerMissing"
    ;;
  NET-02)
    for _ in {1..18}; do
      if remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_rds_tcp_success{job="moodle_node",instance="moodle-app-b"} == 0 and on() (moodle_node_rds_tcp_success{job="moodle_node",instance="moodle-app-a"} == 1) and on() (probe_success{job="blackbox_moodle_rds"} == 1)'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'; then
        observed=true
        observation="only app-b lost container-to-RDS TCP 5432 while app-a and monitor-to-RDS stayed healthy"
        expected_alert="MoodleNodeRdsTcpFailed"
        expected_instance="moodle-app-b"
        break
      fi
      sleep 5
    done
    ;;
  NET-03)
    if remote moodle-app-b 'sudo test -r /var/lib/moodle-faults/NET-03.netem'; then
      run_synthetic_once >/dev/null 2>&1 || { echo 'NET-03 synthetic transaction did not remain healthy' >&2; exit 1; }
      for _ in {1..18}; do
        if remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_rds_tcp_duration_seconds{job="moodle_node",instance="moodle-app-b"} > 0.2 and on() (moodle_node_rds_tcp_duration_seconds{job="moodle_node",instance="moodle-app-a"} < 0.2) and on() (probe_success{job="blackbox_moodle_rds"} == 1)'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'; then
          if remote moodle-app-b 'true'; then
            observed=true
            observation="only app-b container-to-RDS TCP latency exceeded 200 ms; app-a, monitor-to-RDS and host SSH remained healthy"
            expected_alert="MoodleNodeRdsTcpLatencyHigh"
            expected_instance="moodle-app-b"
            break
          fi
        fi
        sleep 5
      done
    fi
    ;;
  CON-02)
    if remote moodle-app-b 'sudo docker exec release-moodle-web-1 test ! -e /etc/apache2/conf-enabled/moodle-router.conf'; then
      for _ in {1..18}; do
        if remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_router_fallback_success{job="moodle_node",instance="moodle-app-b"} == 0 and on() (moodle_node_router_fallback_success{job="moodle_node",instance="moodle-app-a"} == 1)'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'; then
          observed=true
          observation="app-b Apache router disabled and Moodle fallback probe failed while app-a remained healthy"
          expected_alert="MoodleApacheRouterMissing"
          expected_instance="moodle-app-b"
          break
        fi
        sleep 5
      done
    fi
    ;;
  CON-03)
    if remote moodle-app-b 'sudo grep -qx "MOODLE_WWWROOT=invalid-con03-host" /opt/moodle/release/moodle-runtime.env'; then
      for _ in {1..18}; do
        if remote moodle-app-b 'test "$(sudo docker inspect --format "{{.RestartCount}}" release-moodle-web-1)" -ge 2'; then
          observed=true
          observation="app-b Moodle web container restarted at least twice with invalid WWWROOT"
          expected_alert="MoodleWebContainerRestarting"
          expected_instance="moodle-app-b"
          break
        fi
        sleep 5
      done
    fi
    ;;
  SEC-03)
    if remote moodle-app-b 'sudo grep -qx "MOODLE_REVERSE_PROXY=true" /opt/moodle/release/moodle-runtime.env'; then
      for _ in {1..18}; do
        if remote monitor-ai-01 'curl --fail --silent --get --data-urlencode '\''query=moodle_node_reverse_proxy_enabled{job="moodle_node",instance="moodle-app-b"} == 1 and on() (moodle_node_reverse_proxy_enabled{job="moodle_node",instance="moodle-app-a"} == 0)'\'' http://127.0.0.1:9090/api/v1/query | jq -e '\''.data.result | length > 0'\'' >/dev/null'; then
          observed=true
          observation="only app-b reverse-proxy trust drifted from the approved runtime setting"
          expected_alert="MoodleTrustedProxyConfigDrift"
          expected_instance="moodle-app-b"
          break
        fi
        sleep 5
      done
    fi
    ;;
esac

[[ "$observed" == true ]] || { echo "$scenario expected symptom was not observed: $observation" >&2; exit 1; }
wait_for_prometheus_alert "$expected_alert" 150 "$expected_instance"
"$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null
if [[ "$scenario" == RES-02 ]]; then
  for _ in {1..12}; do
    res02_memory_recovered_kib="$(remote moodle-app-b 'awk "/^MemAvailable:/ {print \$2}" /proc/meminfo')"
    if (( res02_memory_recovered_kib >= res02_memory_before_kib - 131072 && res02_memory_recovered_kib >= res02_memory_during_kib + 32768 )); then
      break
    fi
    sleep 5
  done
  (( res02_memory_recovered_kib >= res02_memory_before_kib - 131072 && res02_memory_recovered_kib >= res02_memory_during_kib + 32768 )) || {
    echo "RES-02 host available memory did not recover to the bounded baseline range" >&2
    exit 1
  }
fi
"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
if [[ -n "$expected_instance" ]]; then
  for _ in {1..18}; do
    if remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=ALERTS{alertname=\"$expected_alert\",alertstate=\"firing\",instance=\"$expected_instance\"}' http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length == 0' >/dev/null"; then
      break
    fi
    sleep 5
  done
fi
scenario_verification="$("$script_dir/moodle-scenario-verify.sh" "$scenario")"
jq -e '.status == "passed" and all(.checks[]; .status == "passed")' <<<"$scenario_verification" >/dev/null
reset_needed=false

jq -n --arg run_id "$run_id" --arg scenario "$scenario" --arg observation "$observation" --arg alert "$expected_alert" --arg alert_instance "$expected_instance" --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg before "$res02_memory_before_kib" --arg during "$res02_memory_during_kib" --arg recovered "$res02_memory_recovered_kib" --argjson scenario_verification "$scenario_verification" '{run_id:$run_id,scenario_id:$scenario,status:"passed",method:"manual",evidence_class:"staging_runtime_manual_trial",execution_authority:"allowlisted_test_harness",observation:$observation,prometheus_alert:$alert,prometheus_alert_instance:(if $alert_instance == "" then null else $alert_instance end),reset:"passed",baseline_after_reset:"passed",scenario_verification:$scenario_verification,host_memory_available_kib:(if $before == "" then null else {before:($before|tonumber),during:($during|tonumber),recovered:($recovered|tonumber)} end),completed_at:$completed_at}' > "$result_file"
chmod 0600 "$result_file"
echo "$scenario trial passed; evidence: $result_file"
