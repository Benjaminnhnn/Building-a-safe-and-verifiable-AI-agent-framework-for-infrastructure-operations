#!/usr/bin/env bash
# Run reproducible, safety-gated live Moodle Sprint 2 drills in staging only.
#
# One iteration performs baseline -> inject -> observe alert -> allowlisted
# pipeline reset -> independent verification -> 120-second stability check.
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

run_pipeline_reset() {
  local scenario="$1" run_id="$2" evidence_dir="$3"
  PYTHONPATH="$repo_root/agent_src${PYTHONPATH:+:$PYTHONPATH}" \
  MOODLE_PIPELINE_EXECUTION_CONFIRM=staging \
  python3 - "$scenario" "$run_id" "$evidence_dir" "$repo_root" <<'PY'
import json
import sys
from pathlib import Path

from core.event_schema import normalize_alert
from core.moodle_pipeline import MoodleExecutionAdapter, MoodleIncidentPipeline

scenario_id, run_id, evidence_dir, repo_root = sys.argv[1:]
event = normalize_alert(
    {
        "status": "firing",
        "fingerprint": f"live-{run_id}",
        "labels": {
            "alertname": "MoodleSprint2LiveDrill",
            "scenario_id": scenario_id,
            "environment": "staging",
            "severity": "critical",
        },
        "annotations": {"summary": f"Live drill signal for {scenario_id}"},
    },
    correlation_id=run_id,
)
adapter = MoodleExecutionAdapter(repo_root=Path(repo_root), allow_live_execution=True)
pipeline = MoodleIncidentPipeline(Path(evidence_dir), adapter=adapter)
report = pipeline.process(event, scenario_id=scenario_id, mode="execute", live_verify=True)
if report["state"] != "RESOLVED":
    raise SystemExit(f"pipeline did not resolve {scenario_id}: {report['state']}")
print(json.dumps(report, indent=2))
PY
}

run_one() (
  local scenario="$1" sequence="$2" run_id run_dir result pipeline_report evidence_dir
  local t_inject t_detect t_execute_start t_execute_end t_verify t_resolved stability_started
  local mttd recovery status fault_active=false
  run_id="${scenario}-run${sequence}-$(date -u +%Y%m%dT%H%M%SZ)"
  run_dir="$live_artifacts/$run_id"
  result="$run_dir/result.json"
  evidence_dir="$run_dir/pipeline-evidence"
  pipeline_report="$run_dir/pipeline-report.json"
  mkdir -p "$run_dir"

  cleanup() {
    if [[ "$fault_active" == true ]]; then
      "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null || true
    fi
  }
  trap cleanup EXIT

  "$script_dir/moodle-environment-baseline.sh" verify >/dev/null
  MOODLE_FAULT_CONFIRM=staging "$script_dir/moodle-fault-inject.sh" "$scenario" >/dev/null
  # Detection time starts only once the injector has confirmed the fault is
  # installed, not while it is still establishing an SSH connection/rule.
  t_inject="$(now_epoch)"
  fault_active=true
  observe_expected_symptom "$scenario"
  wait_for_prometheus_alert_since "$(expected_alert "$scenario")" "$t_inject"
  t_detect="$(now_epoch)"

  t_execute_start="$(now_epoch)"
  run_pipeline_reset "$scenario" "$run_id" "$evidence_dir" > "$pipeline_report"
  t_execute_end="$(now_epoch)"
  fault_active=false
  t_verify="$(jq -r 'select(.kind == "verification") | .observed_at' "$evidence_dir/evidence.jsonl" | tail -1)"
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
    --arg t_execute_start "$(epoch_to_iso "$t_execute_start")" \
    --arg t_execute_end "$(epoch_to_iso "$t_execute_end")" \
    --arg t_verify "$t_verify" --arg t_resolved "$(epoch_to_iso "$t_resolved")" \
    --argjson mttd_seconds "$mttd" --argjson recovery_seconds "$recovery" \
    --argjson stability_seconds "$stability_seconds" \
    --arg pipeline_report "$pipeline_report" \
    '{run_id:$run_id,scenario_id:$scenario_id,status:$status,prometheus_alert:$alert,t_inject:$t_inject,t_detect:$t_detect,t_execute_start:$t_execute_start,t_execute_end:$t_execute_end,t_verify:$t_verify,t_resolved:$t_resolved,mttd_seconds:$mttd_seconds,recovery_seconds:$recovery_seconds,stability_seconds:$stability_seconds,baseline_after_stability:"passed",pipeline_report:$pipeline_report}' \
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
