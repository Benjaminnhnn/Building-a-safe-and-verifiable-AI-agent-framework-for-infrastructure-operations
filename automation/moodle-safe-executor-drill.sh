#!/usr/bin/env bash
# Execute the reviewed CON-01 staging action through the Agent two-approval API.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
source "$script_dir/lib/moodle-fault-common.sh"

scenario="${1:-}"
[[ "$scenario" == CON-01 ]] || {
  echo "This integrated Agent handoff drill is limited to CON-01." >&2
  exit 64
}
[[ "${MOODLE_SAFE_EXECUTOR_CONFIRM:-}" == staging ]] || {
  echo "Set MOODLE_SAFE_EXECUTOR_CONFIRM=staging to authorize one staging drill." >&2
  exit 77
}
load_moodle_environment
secrets_dir="$artifacts_dir/safe-executor"
evidence_dir="$artifacts_dir/moodle-safe-executor"
image_file="$artifacts_dir/moodle-agent-image"
run_id="$scenario-$(date -u +%Y%m%dT%H%M%SZ)"
drill_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
evidence_file="$evidence_dir/$run_id.json"
prepared_file="$evidence_dir/$run_id-prepared.json"
gate_approval_file="$evidence_dir/$run_id-gate-approval.json"
executor_approval_file="$evidence_dir/$run_id-executor-approval.json"
execute_body_file="$evidence_dir/$run_id-execute-body.json"
execute_result_file="$evidence_dir/$run_id-execute-result.json"
verifier_file="$evidence_dir/$run_id-verifier.json"
verifier_attestation_file="$evidence_dir/$run_id-verifier-attestation.json"
runtime_verification_file="$evidence_dir/$run_id-runtime-verification.json"
audit_file="$evidence_dir/$run_id-audit.sqlite3"
verifier_signing_key="${MOODLE_VERIFIER_SIGNING_KEY_FILE:-}"
umask 077
mkdir -p "$evidence_dir"
chmod 0700 "$evidence_dir"
for secret_file in \
  "$secrets_dir/agent-hmac.key" \
  "$secrets_dir/safe-action-api-token" \
  "$secrets_dir/approval-ed25519.pub" \
  "$secrets_dir/gate-approval-ed25519.pub" \
  "$secrets_dir/verifier-ed25519.pub"; do
  [[ -r "$secret_file" ]] || { echo "Missing drill credential: $secret_file" >&2; exit 66; }
done
[[ -n "$verifier_signing_key" && -r "$verifier_signing_key" ]] || {
  echo "Set MOODLE_VERIFIER_SIGNING_KEY_FILE to the independent verifier's signing key." >&2
  exit 66
}
[[ -r "$image_file" ]] || { echo "Missing immutable deployed Agent image reference: $image_file" >&2; exit 66; }

agent_live_enabled=false
executor_live_enabled=false
fault_injected=false
agent_tunnel_pid=""
stage=preflight
cleanup() {
  local cleanup_status=$?
  if [[ "$fault_injected" == true ]]; then
    "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null 2>&1 || cleanup_status=1
    remove_scenario_marker "$scenario" >/dev/null 2>&1 || cleanup_status=1
  fi
  if [[ -n "$agent_tunnel_pid" ]]; then
    kill "$agent_tunnel_pid" >/dev/null 2>&1 || true
    wait "$agent_tunnel_pid" 2>/dev/null || true
  fi
  if [[ "$executor_live_enabled" == true ]]; then
    SAFE_EXECUTOR_LIVE_ENABLED=false bash "$script_dir/configure-moodle-safe-executor-api.sh" >/dev/null || cleanup_status=1
  fi
  if [[ "$agent_live_enabled" == true ]]; then
    MOODLE_SAFE_ACTION_LIVE_ENABLED=false bash "$script_dir/configure-moodle-monitoring.sh" >/dev/null || cleanup_status=1
  fi
  if [[ "$cleanup_status" -ne 0 ]]; then
    echo "Drill failed at stage '$stage' (exit $cleanup_status); inspect staging health before retrying." >&2
  fi
  return "$cleanup_status"
}
trap cleanup EXIT

# Verify the AWS caller is operating in the account that owns this staging ALB.
caller_account="$(aws sts get-caller-identity --profile "${AWS_PROFILE:-target-account}" --query Account --output text)"
alb_account="$(cut -d: -f5 <<<"$target_group_arn")"
[[ "$caller_account" == "$alb_account" ]] || {
  echo "AWS account mismatch: caller=$caller_account staging_alb=$alb_account" >&2
  exit 77
}
"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
wait_for_alb_healthy 2
remote moodle-app-b \
  "sudo -n test -r /opt/moodle/release/moodle-runtime.env && sudo -n docker compose --env-file /opt/moodle/release/moodle-runtime.env -f /opt/moodle/release/docker-compose.yml config --quiet"

stage=preflight-kill-switches
ssh -F "$ssh_config" -N -L 127.0.0.1:18000:127.0.0.1:8000 monitor-ai-01 &
agent_tunnel_pid=$!
for _ in {1..30}; do
  if curl --fail --silent http://127.0.0.1:18000/health >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:18000/health \
  | jq -e '.safe_action_live_enabled == false' >/dev/null
remote monitor-ai-01 \
  "curl --fail --silent http://172.17.0.1:8765/healthz | jq -e '.execution_enabled == false' >/dev/null"

stage=enable-executor-switch
executor_live_enabled=true
SAFE_EXECUTOR_LIVE_ENABLED=true SAFE_EXECUTOR_LIVE_CONFIRM=staging \
  bash "$script_dir/configure-moodle-safe-executor-api.sh" >/dev/null
stage=enable-agent-switch
agent_live_enabled=true
MOODLE_SAFE_ACTION_LIVE_ENABLED=true MOODLE_SAFE_ACTION_LIVE_CONFIRM=staging \
  bash "$script_dir/configure-moodle-monitoring.sh" >/dev/null

curl --fail --silent http://127.0.0.1:18000/health >/dev/null
agent_token="$(<"$secrets_dir/safe-action-api-token")"

stage=fault-injection
fault_injected=true
MOODLE_FAULT_CONFIRM=staging "$script_dir/moodle-fault-inject.sh" "$scenario"

stage=observe-fault
one_target=false
for _ in {1..18}; do
  healthy_count="$(aws elbv2 describe-target-health --profile "${AWS_PROFILE:-target-account}" --region "$region" --target-group-arn "$target_group_arn" --query 'TargetHealthDescriptions[?TargetHealth.State==`healthy`]' --output json | jq length)"
  if [[ "$healthy_count" -eq 1 ]]; then one_target=true; break; fi
  sleep 5
done
[[ "$one_target" == true ]] || { echo "Expected one healthy ALB target, found $healthy_count." >&2; exit 1; }
alert_name=MoodleWebContainerMissing
wait_for_prometheus_alert "$alert_name" 150

candidate_id=""
for _ in {1..30}; do
  candidate_id="$(remote monitor-ai-01 "sudo docker exec moodle-ai-agent python -c 'import sqlite3,sys; db=sqlite3.connect(\"/app/data/evidence.db\"); row=db.execute(\"select id from evidence where source=? and json_extract(metadata_json, ?) = ? and json_extract(json_extract(metadata_json, ?), ?) = ? and julianday(collected_at) >= julianday(?) order by collected_at desc limit 1\", (\"ai_decision\", \"$.decision_kind\", \"remediation_candidate\", \"$.decision_json\", \"$.scenario_id\", \"CON-01\", sys.argv[1])).fetchone(); print(row[0] if row else \"\")' '$drill_started_at'")"
  [[ -n "$candidate_id" ]] && break
  sleep 5
done
[[ -n "$candidate_id" ]] || { echo "No evidence-linked CON-01 candidate was created; execution stopped." >&2; exit 1; }

stage=prepare
curl --fail --silent --show-error \
  -H "Authorization: Bearer $agent_token" \
  -X POST "http://127.0.0.1:18000/api/moodle/remediation/$candidate_id/prepare" \
  > "$prepared_file"
jq -e --arg scenario "$scenario" \
  '.status == "approval_required" and .request.scenario_id == $scenario and .request.target_scope == "moodle-app-b" and .execution_permitted == false' \
  "$prepared_file" >/dev/null
request_id="$(jq -r '.request.request_id' "$prepared_file")"
idempotency_key="$(jq -r '.request.idempotency_key' "$prepared_file")"
catalog_action_id="$(jq -r '.request.catalog_action_id' "$prepared_file")"

stage=two-independent-approvals
cat <<EOF
Two separate approvers must sign the prepared request with their own private keys.
Prepared request: $prepared_file
Safety Gate approval digest: $(jq -r '.gate_action_sha256' "$prepared_file")
Executor approval digest: $(jq -r '.executor_action_sha256' "$prepared_file")
Place the signed Gate approval JSON at: $gate_approval_file
Place the signed Executor approval JSON at: $executor_approval_file
EOF
[[ -t 0 ]] || { echo "Interactive approval handoff requires a terminal." >&2; exit 64; }
read -r -t 300 -p "Press Enter after both independent approvers have supplied their signed files: " || {
  echo "Approval handoff timed out; no action was dispatched." >&2
  exit 124
}
[[ -r "$gate_approval_file" && -r "$executor_approval_file" ]] || {
  echo "Both signed approval files are required; no action was dispatched." >&2
  exit 66
}
gate_actor_id="$(jq -er '.actor_id | select(type == "string" and length > 0)' "$gate_approval_file")"
executor_actor_id="$(jq -er '.approval.actor_id | select(type == "string" and length > 0)' "$executor_approval_file")"
[[ "$gate_actor_id" != "$executor_actor_id" ]] || {
  echo "Safety Gate and Executor approvals must have different actor IDs." >&2
  exit 77
}
jq -e --arg digest "$(jq -r '.gate_action_sha256' "$prepared_file")" \
  '.action_sha256 == $digest and (.signature | type == "string" and length > 0)' \
  "$gate_approval_file" >/dev/null
jq -e --arg digest "$(jq -r '.executor_action_sha256' "$prepared_file")" \
  --arg scenario "$scenario" --arg action "$catalog_action_id" \
  --arg idempotency "$idempotency_key" \
  '.scenario_id == $scenario and .catalog_action_id == $action and .idempotency_key == $idempotency and .action_sha256 == $digest and .approval.action_sha256 == $digest and (.approval.signature | type == "string" and length > 0)' \
  "$executor_approval_file" >/dev/null
jq -n \
  --slurpfile prepared "$prepared_file" \
  --slurpfile gate "$gate_approval_file" \
  --slurpfile executor "$executor_approval_file" \
  '{request:$prepared[0].request,gate_approval:$gate[0],executor_approval:$executor[0].approval}' \
  > "$execute_body_file"
chmod 0600 "$prepared_file" "$gate_approval_file" "$executor_approval_file" "$execute_body_file"

stage=agent-execute
curl --fail --silent --show-error \
  -H "Authorization: Bearer $agent_token" -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:18000/api/moodle/remediation/$candidate_id/execute" \
  --data-binary "@$execute_body_file" > "$execute_result_file"
jq -e --arg scenario "$scenario" --arg request_id "$request_id" \
  '.status == "awaiting_verification" and .result.status == "executed" and .result.scenario_id == $scenario and .result.request_id == $request_id and .result.target_scope == "moodle-app-b" and .result.mutated == true' \
  "$execute_result_file" >/dev/null
incident_id="$(jq -r '.incident_id' "$prepared_file")"
remove_scenario_marker "$scenario"
fault_injected=false

stage=pre-verifier-recovery
wait_for_alb_healthy 2
run_synthetic_once >/dev/null
"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
active_alert=active
for _ in {1..20}; do
  active_alert="$(remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\"}' http://127.0.0.1:9090/api/v1/query | jq -r 'if (.data.result | length) > 0 then \"active\" else \"clear\" end'")"
  [[ "$active_alert" == clear ]] && break
  sleep 5
done
[[ "$active_alert" == clear ]] || { echo "Alert did not resolve after the executed action." >&2; exit 1; }

stage=export-audit-and-independent-verification
python3 "$script_dir/export-moodle-safe-action-audit.py" \
  --ssh-config "$ssh_config" --output "$audit_file" >/dev/null
AWS_PROFILE="${AWS_PROFILE:-target-account}" python3 "$script_dir/moodle-independent-verifier.py" \
  --audit-db "$audit_file" --signing-key "$verifier_signing_key" \
  --incident-id "$incident_id" --request-id "$request_id" \
  --scenario-id "$scenario" --window-seconds 120 --interval-seconds 30 \
  > "$verifier_file"
jq -e '.verdict == "ready_for_runtime_acceptance" and .resolution_eligible == true and (.probe_samples | length >= 5) and (.attestation.signature | type == "string" and length > 0)' "$verifier_file" >/dev/null
jq '.attestation' "$verifier_file" > "$verifier_attestation_file"
chmod 0600 "$verifier_file" "$verifier_attestation_file"

stage=persist-runtime-verification
curl --fail --silent --show-error -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:18000/api/moodle/verifications/$request_id" \
  --data-binary "@$verifier_attestation_file" > "$runtime_verification_file"
curl --fail --silent --show-error \
  -H "Authorization: Bearer $agent_token" \
  "http://127.0.0.1:18000/api/moodle/incidents/$incident_id/requests/$request_id/status" \
  > "$runtime_verification_file.status.json"
jq -e --arg incident "$incident_id" --arg request "$request_id" \
  '.status == "resolved" and .incident_id == $incident and .request_id == $request and (.verification_event_id | type == "string" and length > 0) and (.verification_event_hash | type == "string" and length == 64)' \
  "$runtime_verification_file.status.json" >/dev/null
jq -e --arg event_id "$(jq -r '.verification_event_id' "$runtime_verification_file.status.json")" \
  --arg event_hash "$(jq -r '.verification_event_hash' "$runtime_verification_file.status.json")" \
  '.status == "resolved" and .idempotent == false and .event_id == $event_id and .event_hash == $event_hash' \
  "$runtime_verification_file" >/dev/null

stage=disable-live-switches
SAFE_EXECUTOR_LIVE_ENABLED=false bash "$script_dir/configure-moodle-safe-executor-api.sh" >/dev/null
executor_live_enabled=false
MOODLE_SAFE_ACTION_LIVE_ENABLED=false bash "$script_dir/configure-moodle-monitoring.sh" >/dev/null
agent_live_enabled=false
remote monitor-ai-01 \
  "curl --fail --silent http://172.17.0.1:8765/healthz | jq -e '.execution_enabled == false' >/dev/null"
curl --fail --silent http://127.0.0.1:18000/health \
  | jq -e '.safe_action_live_enabled == false' >/dev/null

jq -n \
  --arg run_id "$run_id" --arg scenario "$scenario" --arg alert "$alert_name" \
  --arg candidate_id "$candidate_id" --arg incident_id "$incident_id" \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --slurpfile prepared "$prepared_file" --slurpfile gate "$gate_approval_file" \
  --slurpfile executor_approval "$executor_approval_file" \
  --slurpfile execution "$execute_result_file" --slurpfile verifier "$verifier_file" \
  --slurpfile runtime "$runtime_verification_file" --slurpfile runtime_status "$runtime_verification_file.status.json" \
  '{run_id:$run_id,scenario_id:$scenario,status:"passed",evidence_class:"operator_approved_staging_drill",method:"agent_prepare_two_approvals_execute_signed_independent_verify",observed_alert:$alert,candidate_id:$candidate_id,incident_id:$incident_id,prepared_request:$prepared[0],gate_approval:$gate[0],executor_approval:$executor_approval[0].approval,execution_receipt:$execution[0].result,verifier:$verifier[0],verifier_attestation:$verifier[0].attestation,runtime_persistence:$runtime[0],runtime_status:$runtime_status[0],probe_samples:$verifier[0].probe_samples,alert_resolved:"passed",reset:"safe_executor_receipt",baseline_after_reset:"passed",live_switch:"disabled_after_drill",completed_at:$completed_at}' \
  > "$evidence_file"
chmod 0600 "$evidence_file"
stage=complete
echo "$scenario Agent handoff drill passed; evidence: $evidence_file"
