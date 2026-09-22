#!/usr/bin/env bash
# Run reproducible, safety-gated live Moodle Sprint 2 drills in staging only.
#
# One iteration performs baseline -> inject -> observe alert -> unified AI
# shadow evidence -> allowlisted harness reset -> independent verification ->
# 120-second stability check.  The AI shadow never executes remediation.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

usage() {
  cat <<'USAGE'
Usage: moodle-sprint2-live-drill.sh <DB-01|RES-01|NET-01|CON-01|SEC-02|all> [--runs 3] [--stability-seconds 120]

Runs only the five reviewed Moodle staging faults. It never accepts an
arbitrary command or target. Results are written under a timestamped campaign
in terraform/.artifacts/moodle-sprint2-live/.
USAGE
}

target="${1:-}"
[[ -n "$target" ]] || { usage >&2; exit 64; }
shift || true
runs=3
stability_seconds=120
while (($#)); do
  case "$1" in
    --runs) runs="${2:-}"; shift 2 ;;
    --stability-seconds) stability_seconds="${2:-}"; shift 2 ;;
    *) usage >&2; exit 64 ;;
  esac
done
[[ "$runs" =~ ^[1-9][0-9]*$ ]] || { echo "--runs must be a positive integer" >&2; exit 64; }
[[ "$stability_seconds" =~ ^[1-9][0-9]*$ ]] || { echo "--stability-seconds must be a positive integer" >&2; exit 64; }

if [[ "$target" == all ]]; then
  scenarios=(DB-01 RES-01 NET-01 CON-01 SEC-02)
else
  validate_scenario "$target"
  scenarios=("$target")
fi

command -v python3 >/dev/null 2>&1 || { echo "Required command not found: python3" >&2; exit 127; }
load_moodle_environment
campaign_id="${MOODLE_SPRINT2_CAMPAIGN:-campaign-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$campaign_id" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || { echo "invalid MOODLE_SPRINT2_CAMPAIGN" >&2; exit 64; }
live_artifacts="$artifacts_dir/moodle-sprint2-live/$campaign_id"
umask 077
mkdir -p "$live_artifacts"

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date -u +%s; }
epoch_to_iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }

observe_expected_symptom() {
  local scenario="$1" healthy_count
  case "$scenario" in
    DB-01|NET-01|SEC-02)
      if run_synthetic_once >/dev/null 2>&1; then
        echo "$scenario: synthetic transaction unexpectedly passed" >&2
        return 1
      fi
      ;;
    RES-01)
      remote moodle-app-b 'test "$(sudo docker inspect --format "{{.State.Running}}" moodle-fault-res-01)" = true'
      ;;
    CON-01)
      for _ in {1..18}; do
        healthy_count="$(aws elbv2 describe-target-health --profile "${AWS_PROFILE:-target-account}" --region "$region" --target-group-arn "$target_group_arn" --query 'TargetHealthDescriptions[?TargetHealth.State==`healthy`]' --output json | jq length)"
        [[ "$healthy_count" -eq 1 ]] && return 0
        sleep 5
      done
      echo "CON-01: expected exactly one healthy ALB target" >&2
      return 1
      ;;
  esac
}

expected_alert() {
  case "$1" in
    DB-01|NET-01|SEC-02) echo MoodleSyntheticTransactionFailed ;;
    RES-01) echo MoodleNodeCpuHigh ;;
    CON-01) echo MoodleWebContainerMissing ;;
  esac
}

wait_for_prometheus_alert_since() {
  local alert_name="$1" injected_at="$2" timeout_seconds=150 elapsed=0 active_at active_epoch
  while (( elapsed < timeout_seconds )); do
    active_at="$(remote monitor-ai-01 "curl --fail --silent http://127.0.0.1:9090/api/v1/alerts | jq -r --arg alert '$alert_name' '.data.alerts[] | select(.labels.alertname == \$alert and .state == \"firing\") | .activeAt' | head -1")"
    if [[ -n "$active_at" && "$active_at" != null ]]; then
      active_epoch="$(date -u -d "$active_at" +%s)"
      if (( active_epoch >= injected_at )); then
        return 0
      fi
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  echo "Prometheus alert did not fire after fault injection: $alert_name" >&2
  return 1
}

shadow_observed_count() {
  remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=sum(aiops_unified_shadow_events_total{status=\"observed\"})' http://127.0.0.1:9090/api/v1/query | jq -r '.data.result[0].value[1] // \"0\"'"
}

wait_for_ai_shadow_observation() {
  local before="$1" timeout_seconds=180 elapsed=0 current
  while (( elapsed < timeout_seconds )); do
    current="$(shadow_observed_count)"
    if awk -v current="$current" -v before="$before" 'BEGIN {exit !(current > before)}'; then
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  echo "Unified AI shadow did not record the Alertmanager event within ${timeout_seconds}s." >&2
  echo "Redeploy the merged agent and monitoring playbook with AIOPS_UNIFIED_CORE_MODE=shadow." >&2
  return 1
}

run_controlled_reset() {
  local scenario="$1" run_id="$2" report_path="$3"
  "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null
  jq -n \
    --arg run_id "$run_id" --arg scenario_id "$scenario" \
    '{run_id:$run_id,scenario_id:$scenario_id,ai_layer_mode:"shadow",ai_execution:false,execution_authority:"allowlisted_test_harness",action:"moodle-fault-reset",status:"executed",mutated:true,evidence_store:"monitor-ai-01:moodle-ai-agent:/app/data/evidence.db"}' \
    > "$report_path"
}

run_one() (
  local scenario="$1" sequence="$2" run_id run_dir result pipeline_report
  local t_inject t_detect t_ai_observed t_execute_start t_execute_end t_verify t_resolved stability_started
  local mttd recovery status shadow_before fault_active=false
  run_id="${scenario}-run${sequence}-$(date -u +%Y%m%dT%H%M%SZ)"
  run_dir="$live_artifacts/$run_id"
  result="$run_dir/result.json"
  pipeline_report="$run_dir/pipeline-report.json"
  mkdir -p "$run_dir"

  cleanup() {
    if [[ "$fault_active" == true ]]; then
      "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null || true
    fi
  }
  trap cleanup EXIT

  "$script_dir/moodle-environment-baseline.sh" verify >/dev/null
  shadow_before="$(shadow_observed_count)"
  MOODLE_FAULT_CONFIRM=staging "$script_dir/moodle-fault-inject.sh" "$scenario" >/dev/null
  # Detection time starts only once the injector has confirmed the fault is
  # installed, not while it is still establishing an SSH connection/rule.
  t_inject="$(now_epoch)"
  fault_active=true
  observe_expected_symptom "$scenario"
  wait_for_prometheus_alert_since "$(expected_alert "$scenario")" "$t_inject"
  t_detect="$(now_epoch)"
  wait_for_ai_shadow_observation "$shadow_before"
  t_ai_observed="$(now_epoch)"

  t_execute_start="$(now_epoch)"
  run_controlled_reset "$scenario" "$run_id" "$pipeline_report"
  t_execute_end="$(now_epoch)"
  fault_active=false
  "$script_dir/moodle-environment-baseline.sh" verify >/dev/null
  t_verify="$(now_iso)"
  t_resolved="$(now_epoch)"
  stability_started="$(now_epoch)"
  sleep "$stability_seconds"
  "$script_dir/moodle-environment-baseline.sh" verify >/dev/null

  mttd=$((t_detect - t_inject))
  recovery=$((t_resolved - t_detect))
  status=passed
  (( mttd <= 60 && recovery <= 600 )) || status=failed_slo
  jq -n \
    --arg run_id "$run_id" --arg scenario_id "$scenario" --arg status "$status" \
    --arg alert "$(expected_alert "$scenario")" \
    --arg t_inject "$(epoch_to_iso "$t_inject")" \
    --arg t_detect "$(epoch_to_iso "$t_detect")" \
    --arg t_ai_observed "$(epoch_to_iso "$t_ai_observed")" \
    --arg t_execute_start "$(epoch_to_iso "$t_execute_start")" \
    --arg t_execute_end "$(epoch_to_iso "$t_execute_end")" \
    --arg t_verify "$t_verify" --arg t_resolved "$(epoch_to_iso "$t_resolved")" \
    --argjson mttd_seconds "$mttd" --argjson recovery_seconds "$recovery" \
    --argjson stability_seconds "$stability_seconds" \
    --arg pipeline_report "$pipeline_report" \
    '{run_id:$run_id,scenario_id:$scenario_id,status:$status,prometheus_alert:$alert,t_inject:$t_inject,t_detect:$t_detect,t_ai_observed:$t_ai_observed,ai_layer_mode:"shadow",ai_shadow_observed:"passed",t_execute_start:$t_execute_start,t_execute_end:$t_execute_end,t_verify:$t_verify,t_resolved:$t_resolved,mttd_seconds:$mttd_seconds,recovery_seconds:$recovery_seconds,stability_seconds:$stability_seconds,baseline_after_stability:"passed",pipeline_report:$pipeline_report}' \
    > "$result"
  chmod 0600 "$result" "$pipeline_report"
  if [[ "$status" != passed ]]; then
    echo "$scenario run $sequence failed an SLO: $result" >&2
    return 1
  fi
  echo "$scenario run $sequence passed: $result"
)

for scenario in "${scenarios[@]}"; do
  for sequence in $(seq 1 "$runs"); do
    run_one "$scenario" "$sequence"
  done
done

echo "Sprint 2 live drills complete. Results: $live_artifacts"
