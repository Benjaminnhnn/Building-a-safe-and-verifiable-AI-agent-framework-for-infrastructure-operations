#!/usr/bin/env bash
# Run one reviewed S3.3 staging reset through the separately approved API.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
source "$script_dir/lib/moodle-fault-common.sh"

scenario="${1:-}"
case "$scenario" in
  DB-01) expected_nodes='["a","b"]'; alert_name=MoodleSyntheticTransactionFailed ;;
  RES-01) expected_nodes='["b"]'; alert_name=MoodleNodeCpuHigh ;;
  NET-01) expected_nodes='["a","b"]'; alert_name=MoodleSyntheticTransactionFailed ;;
  CON-01) expected_nodes='["b"]'; alert_name=MoodleWebContainerMissing ;;
  SEC-02) expected_nodes='["a"]'; alert_name=MoodleSyntheticTransactionFailed ;;
  *) echo "Scenario must be one of DB-01, RES-01, NET-01, CON-01, SEC-02." >&2; exit 64 ;;
esac
[[ "${MOODLE_SAFE_EXECUTOR_CONFIRM:-}" == staging ]] || {
  echo "Set MOODLE_SAFE_EXECUTOR_CONFIRM=staging to authorize one staging drill." >&2
  exit 77
}
load_moodle_environment
secrets_dir="$artifacts_dir/safe-executor"
evidence_dir="$artifacts_dir/moodle-safe-executor"
run_id="$scenario-$(date -u +%Y%m%dT%H%M%SZ)"
drill_started_at="$(date -u +%Y-%m-%dT%H:%M:%S)"
idempotency_key="$run_id-executor-reset"
approval_file="$evidence_dir/$run_id-approval.json"
evidence_file="$evidence_dir/$run_id.json"
mkdir -p "$evidence_dir"
chmod 0700 "$evidence_dir"
[[ -r "$secrets_dir/agent-hmac.key" && -r "$secrets_dir/approval-ed25519-private.pem" ]]

live_enabled=false
fault_injected=false
tunnel_pid=""
stage=preflight
cleanup() {
  local cleanup_status=$?
  if [[ "$fault_injected" == true ]]; then
    "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null 2>&1 || cleanup_status=1
  fi
  if [[ -n "$tunnel_pid" ]]; then
    kill "$tunnel_pid" >/dev/null 2>&1 || true
    wait "$tunnel_pid" 2>/dev/null || true
  fi
  if [[ "$live_enabled" == true ]]; then
    SAFE_EXECUTOR_LIVE_ENABLED=false bash "$script_dir/configure-moodle-safe-executor-api.sh" >/dev/null || cleanup_status=1
  fi
  if [[ "$cleanup_status" -ne 0 ]]; then
    echo "Drill failed at stage '$stage' (exit $cleanup_status); inspect staging health before retrying." >&2
  fi
  return "$cleanup_status"
}
trap cleanup EXIT

"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
wait_for_alb_healthy 2
python3 "$script_dir/moodle-safe-approval.py" "$scenario" \
  --private-key "$secrets_dir/approval-ed25519-private.pem" \
  --idempotency-key "$idempotency_key" --actor operator --ttl-minutes 15 \
  > "$approval_file"
chmod 0600 "$approval_file"

stage=enable-executor
live_enabled=true
SAFE_EXECUTOR_LIVE_ENABLED=true SAFE_EXECUTOR_LIVE_CONFIRM=staging \
  bash "$script_dir/configure-moodle-safe-executor-api.sh" >/dev/null
stage=api-tunnel
ssh -F "$ssh_config" -N -L 127.0.0.1:18765:172.17.0.1:8765 monitor-ai-01 &
tunnel_pid=$!
for _ in {1..20}; do
  if curl --fail --silent http://127.0.0.1:18765/healthz >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:18765/healthz | jq -e '.execution_enabled == true' >/dev/null

stage=fault-injection
fault_injected=true
MOODLE_FAULT_CONFIRM=staging "$script_dir/moodle-fault-inject.sh" "$scenario"
publish_scenario_marker "$scenario" "$run_id"
stage=observe-fault
case "$scenario" in
  DB-01|NET-01|SEC-02)
    if run_synthetic_once >/dev/null 2>&1; then
      echo "$scenario fault was not observed by the synthetic transaction; aborting remediation." >&2
      exit 1
    fi
    ;;
  RES-01)
    remote moodle-app-b 'test "$(sudo docker inspect --format "{{.State.Running}}" moodle-fault-res-01)" = true'
    ;;
  CON-01)
    one_target=false
    for _ in {1..18}; do
      healthy_count="$(aws elbv2 describe-target-health --profile "${AWS_PROFILE:-target-account}" --region "$region" --target-group-arn "$target_group_arn" --query 'TargetHealthDescriptions[?TargetHealth.State==`healthy`]' --output json | jq length)"
      if [[ "$healthy_count" -eq 1 ]]; then one_target=true; break; fi
      sleep 5
    done
    [[ "$one_target" == true ]] || { echo "Expected one healthy ALB target, found $healthy_count." >&2; exit 1; }
    ;;
esac
wait_for_prometheus_alert "$alert_name" 150
scenario_alert=false
for _ in {1..30}; do
  if remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\"}' http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length > 0' >/dev/null"; then
    scenario_alert=true
    break
  fi
  sleep 5
done
[[ "$scenario_alert" == true ]] || {
  echo "Prometheus did not observe the expected symptom alert: $alert_name" >&2
  exit 1
}

scenario_binding=false
for _ in {1..24}; do
  if remote monitor-ai-01 "sudo docker exec moodle-ai-agent python -c 'import sqlite3,sys; db=sqlite3.connect(\"/app/data/evidence.sqlite3\"); row=db.execute(\"select 1 from evidence where kind=? and json_extract(payload_json, ?) = ? and observed_at >= ? limit 1\", (\"scenario_action_binding\", \"$.scenario_id\", sys.argv[1], sys.argv[2])).fetchone(); sys.exit(0 if row else 1)' '$scenario' '$drill_started_at'"; then
    scenario_binding=true
    break
  fi
  sleep 5
done
[[ "$scenario_binding" == true ]] || {
  echo "Agent did not record the typed scenario/action binding: $scenario" >&2
  exit 1
}

stage=executor-reset
executor_result="$(PYTHONPATH="$repo_root/agent_src" python3 "$script_dir/moodle-safe-executor-request.py" \
  "$approval_file" --endpoint http://127.0.0.1:18765 \
  --hmac-key-file "$secrets_dir/agent-hmac.key" --idempotency-key "$idempotency_key")"
echo "$executor_result" | jq -e --arg scenario "$scenario" --argjson nodes "$expected_nodes" \
  '.status == "executed" and .scenario_id == $scenario and .completed_nodes == $nodes and .mutated == true' >/dev/null
remove_scenario_marker "$scenario"
fault_injected=false

stage=verify-recovery
wait_for_alb_healthy 2
run_synthetic_once >/dev/null
"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
for _ in {1..20}; do
  active_alert="$(remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\"}' http://127.0.0.1:9090/api/v1/query | jq -r 'if (.data.result | length) > 0 then \"active\" else \"clear\" end'")"
  [[ "$active_alert" == clear ]] && break
  sleep 5
done
[[ "${active_alert:-active}" == clear ]] || { echo "Alert did not resolve after reset: $alert_name" >&2; exit 1; }

scenario_verification="$(AWS_PROFILE="${AWS_PROFILE:-target-account}" \
  "$script_dir/moodle-scenario-verify.sh" "$scenario" "$run_id")"
echo "$scenario_verification" | jq -e \
  --arg scenario "$scenario" --arg drill_id "$run_id" \
  '.scenario_id == $scenario and .drill_id == $drill_id and .status == "passed" and all(.checks[]; .status == "passed")' >/dev/null

jq -n \
  --arg run_id "$run_id" \
  --arg scenario "$scenario" \
  --arg alert "$alert_name" \
  --arg scenario_label "$scenario_alert" \
  --arg scenario_binding "$scenario_binding" \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson executor "$executor_result" \
  --argjson scenario_verification "$scenario_verification" \
  '{run_id:$run_id,scenario_id:$scenario,status:"passed",observed_alert:$alert,scenario_label:$scenario_label,scenario_binding:$scenario_binding,executor:$executor,scenario_verification:$scenario_verification,alert_resolved:"passed",reset:"passed",baseline_after_reset:"passed",live_switch:"disabled_after_drill",completed_at:$completed_at}' \
  > "$evidence_file"
chmod 0600 "$evidence_file"
stage=complete
echo "$scenario safe-executor drill passed; evidence: $evidence_file"
