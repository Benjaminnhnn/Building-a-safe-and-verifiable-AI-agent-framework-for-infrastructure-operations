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
# BIZ alerts hold for 5 minutes and SEC-01 waits for the 60-second audit.
alert_timeout_seconds=150

case "$scenario" in
  DB-01|NET-01)
    if run_synthetic_once >/dev/null 2>&1; then
      observation="synthetic transaction unexpectedly passed"
    else
      observed=true
      observation="synthetic transaction failed as expected"
      expected_alert="MoodleSyntheticTransactionFailed"
    fi
    ;;
  RES-01)
    if remote moodle-app-b "test \"\$(sudo docker inspect --format '{{.State.Running}}' moodle-fault-res-01)\" = true"; then
      observed=true
      observation="bounded CPU load container is running"
      expected_alert="MoodleNodeCpuHigh"
    fi
    ;;
  CON-01)
    for _ in {1..18}; do
      healthy_count="$(aws elbv2 describe-target-health --profile "${AWS_PROFILE:-target-account}" --region "$region" --target-group-arn "$target_group_arn" --query 'TargetHealthDescriptions[?TargetHealth.State==`healthy`]' --output json | jq length)"
      if [[ "$healthy_count" -eq 1 ]]; then observed=true; observation="one ALB target became unhealthy while one remained healthy"; break; fi
      sleep 5
    done
    expected_alert="MoodleWebContainerMissing"
    ;;
  BIZ-01)
    if wait_for_prometheus_query 'count(moodle_cli_maintenance_mode_enabled{job="moodle_node"} == 1) == 2 and on() max(moodle_upgrade_in_progress{job="moodle_node"}) == 0' 90; then
      observed=true
      observation="CLI maintenance flag is set on both nodes with no upgrade running while web containers stay up"
      expected_alert="MoodleMaintenanceModeStuck"
      alert_timeout_seconds=420
    fi
    ;;
  BIZ-02)
    if wait_for_prometheus_query 'moodle_cron_container_stopped{job="moodle_node",instance="moodle-app-a"} == 1 and on() max(moodle_adhoc_tasks_overdue_count{job="moodle_node"}) >= 1' 90; then
      observed=true
      observation="moodle-cron is stopped on app-a and at least one ad-hoc task is overdue"
      expected_alert="MoodleAdhocTaskQueueStuck"
      alert_timeout_seconds=420
    fi
    ;;
  SEC-01)
    if wait_for_prometheus_query 'moodle_rds_sg_unreviewed_ingress_rules{job="moodle_synthetic",instance="monitor-ai-01"} >= 1 and on() moodle_rds_sg_public_ingress_rules{job="moodle_synthetic",instance="monitor-ai-01"} == 0' 150; then
      observed=true
      observation="the RDS security group gained one unreviewed source group while no public CIDR was opened"
      expected_alert="MoodleDatabasePortExposure"
      alert_timeout_seconds=180
    fi
    ;;
esac

[[ "$observed" == true ]] || { echo "$scenario expected symptom was not observed: $observation" >&2; exit 1; }
wait_for_prometheus_alert "$expected_alert" "$alert_timeout_seconds" "$expected_instance"
"$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null
"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
if [[ -n "$expected_instance" ]]; then
  for _ in {1..18}; do
    if remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=ALERTS{alertname=\"$expected_alert\",alertstate=\"firing\",instance=\"$expected_instance\"}' http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length == 0' >/dev/null"; then
      break
    fi
    sleep 5
  done
fi
if [[ "$scenario" == SEC-01 ]]; then
  # SEC-01 is human-only; the agent verifier refuses it by design.
  scenario_verification="$("$script_dir/moodle-human-remediation-verify.sh" "$scenario")"
else
  scenario_verification="$("$script_dir/moodle-scenario-verify.sh" "$scenario")"
fi
jq -e '.status == "passed" and all(.checks[]; .status == "passed")' <<<"$scenario_verification" >/dev/null
reset_needed=false

jq -n --arg run_id "$run_id" --arg scenario "$scenario" --arg observation "$observation" --arg alert "$expected_alert" --arg alert_instance "$expected_instance" --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson scenario_verification "$scenario_verification" '{run_id:$run_id,scenario_id:$scenario,status:"passed",method:"manual",evidence_class:"staging_runtime_manual_trial",execution_authority:"allowlisted_test_harness",observation:$observation,prometheus_alert:$alert,prometheus_alert_instance:(if $alert_instance == "" then null else $alert_instance end),reset:"passed",baseline_after_reset:"passed",scenario_verification:$scenario_verification,completed_at:$completed_at}' > "$result_file"
chmod 0600 "$result_file"
echo "$scenario trial passed; evidence: $result_file"
