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
MOODLE_FAULT_CONFIRM=staging "$script_dir/moodle-fault-inject.sh" "$scenario" >/dev/null
reset_needed=true
observed=false
observation=""
expected_alert=""

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
esac

[[ "$observed" == true ]] || { echo "$scenario expected symptom was not observed: $observation" >&2; exit 1; }
wait_for_prometheus_alert "$expected_alert" 150
"$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null
reset_needed=false
"$script_dir/moodle-environment-baseline.sh" verify >/dev/null

jq -n --arg run_id "$run_id" --arg scenario "$scenario" --arg observation "$observation" --arg alert "$expected_alert" --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{run_id:$run_id,scenario_id:$scenario,status:"passed",observation:$observation,prometheus_alert:$alert,reset:"passed",baseline_after_reset:"passed",completed_at:$completed_at}' > "$result_file"
chmod 0600 "$result_file"
echo "$scenario trial passed; evidence: $result_file"
