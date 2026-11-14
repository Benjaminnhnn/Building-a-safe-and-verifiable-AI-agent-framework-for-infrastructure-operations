#!/usr/bin/env bash
# Ablation arm "Agent without Safety Gate, Safe Executor and Independent Verifier"
# for DB-01 and NET-01, with measured RTO and RPO.
#
#   inject -> alert -> Agent diagnosis (shadow mode) -> the Agent's proposed action is
#   applied DIRECTLY by the allowlisted fault-reset harness (no prepare, no Operator
#   signature, no Safe Executor) -> recovery judged by probes only (no Verifier).
#
# RTO: user-facing (t_recovered - t_inject) and operational (t_recovered - t_detect).
#      t_recovered = first success of RTO_STABLE_SAMPLES consecutive authenticated
#      synthetic transactions after the direct action.
# RPO: a heartbeat writer inserts one row every RPO_WRITE_INTERVAL seconds through the
#      web container of node A (the same DB path Moodle uses). After recovery every
#      acknowledged row must still exist. RPO = time span of acknowledged-but-missing
#      rows (0 when nothing acknowledged was lost).
#
# Staging only. This arm deliberately bypasses the safety chain to give a comparison
# baseline; it never runs an action outside the two reviewed reset procedures, and an
# unexpected Agent proposal is recorded and NOT executed (the trap still resets).
#
# Usage (repo root):
#   MOODLE_FAULT_CONFIRM=staging bash automation/moodle-rto-rpo-ablation.sh DB-01|NET-01
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
case "$scenario" in
  DB-01) expected_alert="MoodleNodeRdsTcpFailed"; expected_action="remove_scoped_db_reject" ;;
  NET-01) expected_alert="MoodleSyntheticTransactionFailed"; expected_action="recreate_moodle_web_from_reviewed_compose" ;;
  *) echo "Usage: MOODLE_FAULT_CONFIRM=staging $0 DB-01|NET-01" >&2; exit 64 ;;
esac
[[ "${MOODLE_FAULT_CONFIRM:-}" == staging ]] || { echo "Set MOODLE_FAULT_CONFIRM=staging." >&2; exit 64; }
command -v base64 >/dev/null && command -v awk >/dev/null || { echo "base64 and awk are required." >&2; exit 127; }

probe_interval="${RTO_PROBE_INTERVAL:-2}"
write_interval="${RPO_WRITE_INTERVAL:-2}"
stable_samples="${RTO_STABLE_SAMPLES:-3}"
alert_timeout="${RTO_ALERT_TIMEOUT:-300}"
agent_timeout="${RTO_AGENT_TIMEOUT:-300}"
recovery_timeout="${RTO_RECOVERY_TIMEOUT:-900}"
warmup="${RTO_WARMUP_SECONDS:-30}"

load_moodle_environment
run_id="ABL-${scenario}-$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="$artifacts_dir/moodle-rto-rpo/$run_id"
mkdir -p "$out_dir"
probe_log="$out_dir/login-probe.tsv"       # epoch  http_code
writer_log="$out_dir/heartbeat-writer.tsv" # epoch  seq  ACK|FAIL
hb_plugin="local_rpo_hb"
# lib/moodle-ops-env.sh (ops_translate, MOODLE_WEB_CONTAINER) is not on every
# branch; fall back to the canonical staging names the other drills use.
if declare -F ops_translate >/dev/null; then
  node_a_ssh="$(ops_translate moodle-app-a)"
else
  node_a_ssh="moodle-app-a"
fi
web_container="${MOODLE_WEB_CONTAINER:-release-moodle-web-1}"

now() { date -u +%s.%3N; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%S.%3NZ; }
stage() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

declare -A T
reset_needed=false
probe_pid=""; writer_pid=""

stop_background() {
  [[ -n "$probe_pid" ]] && kill "$probe_pid" 2>/dev/null || true
  if [[ -n "$writer_pid" ]]; then
    ssh -F "$ssh_config" "$node_a_ssh" "touch /tmp/$run_id.stop" >/dev/null 2>&1 || true
    sleep 3; kill "$writer_pid" 2>/dev/null || true
  fi
  probe_pid=""; writer_pid=""
}
cleanup() {
  stop_background
  if [[ "$reset_needed" == true ]]; then
    stage "cleanup: resetting $scenario"
    "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null || true
  fi
}
trap cleanup EXIT

# PHP run inside the Moodle web container; reads the code from stdin.
php_insert='<?php define("CLI_SCRIPT", true); require "/var/www/html/config.php";
$DB->insert_record("config_plugins", (object)["plugin" => "local_rpo_hb", "name" => $argv[1], "value" => $argv[2]]);
echo "ACK\n";'
php_list='<?php define("CLI_SCRIPT", true); require "/var/www/html/config.php";
foreach ($DB->get_records_menu("config_plugins", ["plugin" => "local_rpo_hb"], "", "name,value") as $n => $v) { echo "$n\t$v\n"; }'
php_purge='<?php define("CLI_SCRIPT", true); require "/var/www/html/config.php";
$DB->delete_records("config_plugins", ["plugin" => "local_rpo_hb"]); echo "PURGED\n";'

container_php() { # $1 = php code; remaining args passed to the script
  local code="$1"; shift
  printf '%s' "$code" | ssh -F "$ssh_config" "$node_a_ssh" "sudo docker exec -i $web_container php -- $*"
}

# --- 0. Preconditions --------------------------------------------------------
stage "preflight: baseline, live switches, heartbeat table"
"$script_dir/moodle-environment-baseline.sh" verify >/dev/null
agent_live="$(remote monitor-ai-01 'curl -s --max-time 5 http://127.0.0.1:8000/health' | jq -r '.safe_action_live_enabled // false')" || agent_live=unknown
executor_live="$(remote monitor-ai-01 'curl -s --max-time 5 http://172.17.0.1:8765/healthz' 2>/dev/null | jq -r '.execution_enabled // false' 2>/dev/null)" || executor_live=absent
[[ "$agent_live" != true && "$executor_live" != true ]] || {
  echo "Live switches must be off for this arm (agent_live=$agent_live executor_live=$executor_live)." >&2; exit 77; }
container_php "$php_purge" >/dev/null

# --- 1. Background probes ----------------------------------------------------
(
  while :; do
    code="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$public_url/login/index.php" || true)"
    printf '%s\t%s\n' "$(now)" "${code:-000}"
    sleep "$probe_interval"
  done
) >"$probe_log" &
probe_pid=$!

writer_script="$(cat <<EOF
set -u
seq=0; deadline=\$((\$(date +%s) + 3600)); rm -f /tmp/$run_id.stop
code=\$(printf '%s' '$(printf '%s' "$php_insert" | base64 -w0)' | base64 -d)
while [ ! -e /tmp/$run_id.stop ] && [ \$(date +%s) -lt \$deadline ]; do
  seq=\$((seq + 1)); ts=\$(date -u +%s.%3N)
  if printf '%s' "\$code" | timeout 15 sudo docker exec -i $web_container php -- hb-\$seq \$ts 2>/dev/null | grep -q '^ACK\$'; then r=ACK; else r=FAIL; fi
  printf '%s\t%s\t%s\n' "\$ts" "\$seq" "\$r"
  sleep $write_interval
done
rm -f /tmp/$run_id.stop
EOF
)"
ssh -F "$ssh_config" "$node_a_ssh" "bash -c $(printf '%q' "$writer_script")" >"$writer_log" 2>/dev/null &
writer_pid=$!

stage "warm-up ${warmup}s (probes and heartbeat must be healthy)"
sleep "$warmup"
awk -F'\t' '$2=="200"{ok=1} END{exit !ok}' "$probe_log" || { echo "Login probe never returned 200 during warm-up." >&2; exit 1; }
grep -q $'\tACK$' "$writer_log" || { echo "Heartbeat writer acknowledged nothing during warm-up." >&2; exit 1; }

# --- 2. Inject and detect ----------------------------------------------------
reset_needed=true
T[inject]="$(now)"; stage "① inject $scenario"
MOODLE_FAULT_CONFIRM=staging "$script_dir/moodle-fault-inject.sh" "$scenario" >/dev/null

stage "② waiting for $expected_alert to fire (<= ${alert_timeout}s)"
wait_for_prometheus_alert "$expected_alert" "$alert_timeout"
T[alert_firing_seen]="$(now)"
active_at="$(remote monitor-ai-01 "curl -s http://127.0.0.1:9090/api/v1/alerts" \
  | jq -r --arg a "$expected_alert" '[.data.alerts[] | select(.labels.alertname==$a) | .activeAt] | sort | first // empty')"
T[detect]="$(date -u -d "$active_at" +%s.%3N 2>/dev/null || echo "${T[alert_firing_seen]}")"

# --- 3. Agent diagnosis (shadow mode, no gate) -------------------------------
stage "③ waiting for the Agent's remediation proposal (<= ${agent_timeout}s)"
since_iso="$(date -u -d "@${T[inject]%.*}" +%Y-%m-%dT%H:%M:%S)"
decision=""
for ((elapsed = 0; elapsed < agent_timeout; elapsed += 5)); do
  decision="$(remote monitor-ai-01 "sudo docker exec -i moodle-ai-agent python - $since_iso" <<'PY' || true
import json, os, sqlite3, sys
since = sys.argv[1]
if not os.path.exists("/app/data/evidence.db"):  # a fresh Agent creates it on its first decision
    sys.exit(0)
db = sqlite3.connect("file:/app/data/evidence.db?mode=ro", uri=True)
query = (
    "select evidence_id, incident_id, collected_at, json_extract(metadata_json,'$.decision_json') from evidence"
    " where source='ai_decision' and json_extract(metadata_json,'$.decision_kind')=? order by collected_at asc")
for eid, incident, at, payload in db.execute(query, ("remediation_candidate",)):
    if (at or "")[:19] < since:
        continue
    d = json.loads(payload or "{}")
    if d.get("catalog_action_id"):
        # cause_code and confidence live in the same incident's diagnosis record.
        diag = {}
        for _, diag_incident, diag_at, diag_payload in db.execute(query, ("diagnosis",)):
            if diag_incident == incident and (diag_at or "") <= (at or ""):
                diag = json.loads(diag_payload or "{}")
        print(json.dumps({"evidence_id": eid, "incident_id": incident, "collected_at": at,
            **{k: d.get(k) for k in ("scenario_id", "catalog_action_id", "target_scope")},
            **{k: diag.get(k) for k in ("cause_code", "confidence", "disposition")},
            "evidence_refs": len(d.get("evidence_refs") or [])}))
        break
PY
)"
  [[ -n "$decision" ]] && break
  sleep 5
done
[[ -n "$decision" ]] || { echo "Agent produced no remediation proposal within ${agent_timeout}s." >&2; exit 1; }
T[diagnose]="$(date -u -d "$(jq -r .collected_at <<<"$decision")" +%s.%3N 2>/dev/null || now)"
proposed="$(jq -r .catalog_action_id <<<"$decision")"
stage "   Agent proposed: $proposed ($(jq -c '{scenario_id, cause_code, confidence, evidence_refs}' <<<"$decision"))"
if [[ "$proposed" != "$expected_action" ]]; then
  jq -n --arg s "$scenario" --argjson d "$decision" '{scenario_id:$s,status:"agent_action_mismatch",decision:$d}' >"$out_dir/result.json"
  echo "Agent proposed '$proposed', expected '$expected_action'; not executed. Trap resets the fault." >&2
  exit 1
fi

# --- 4. Direct execution: no Safety Gate, no signature, no Safe Executor -----
T[execute_start]="$(now)"; stage "⑤ executing $proposed directly (no gate/executor)"
"$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null
T[execute_end]="$(now)"
reset_needed=false

# --- 5. Recovery by probes only (no Independent Verifier) --------------------
stage "⑥' waiting for $stable_samples consecutive synthetic successes (no Verifier)"
streak=0; first_ok=""; deadline=$(( ${T[execute_end]%.*} + recovery_timeout ))
while (( $(date +%s) < deadline )); do
  started="$(now)"
  if output="$(run_synthetic_once 2>&1)"; then
    [[ $streak -eq 0 ]] && first_ok="$started"
    streak=$((streak + 1))
    (( streak >= stable_samples )) && break
  elif grep -q 'already running' <<<"$output"; then
    sleep 2; continue
  else
    streak=0; first_ok=""
  fi
  sleep 5
done
(( streak >= stable_samples )) || { echo "Service did not recover within ${recovery_timeout}s after the direct action." >&2; exit 1; }
T[recovered]="$first_ok"
stage "   recovered at $(iso "${T[recovered]}")"
sleep 10
stop_background

# --- 6. RPO: every acknowledged heartbeat must still be in the database ------
container_php "$php_list" >"$out_dir/heartbeat-present.tsv"
container_php "$php_purge" >/dev/null || true
awk -F'\t' '$3=="ACK"{print "hb-"$2"\t"$1}' "$writer_log" | sort >"$out_dir/acked.tsv"
cut -f1 "$out_dir/heartbeat-present.tsv" | sort >"$out_dir/present.names"
join -t$'\t' -v1 "$out_dir/acked.tsv" "$out_dir/present.names" >"$out_dir/lost.tsv" || true
acked=$(wc -l <"$out_dir/acked.tsv"); lost=$(wc -l <"$out_dir/lost.tsv")
rpo=$(awk -F'\t' -v w="$write_interval" 'NR==1{mn=$2;mx=$2} {if($2<mn)mn=$2; if($2>mx)mx=$2} END{if(NR==0)print 0; else printf "%.3f", mx-mn+w}' "$out_dir/lost.tsv")
write_outage=$(awk -F'\t' -v t="${T[inject]}" '$1>=t && $3=="FAIL" && !f{f=$1} f && $3=="ACK" && $1>f{printf "%.3f", $1-f; exit}' "$writer_log")
login_down=$(awk -F'\t' -v t="${T[inject]}" '$1>=t && $2!="200" && !f{f=$1} END{if(f) printf "%.3f", f}' "$probe_log")
login_up=$(awk -F'\t' -v d="$login_down" 'd!="" && $1>d && $2=="200"{printf "%.3f", $1; exit}' "$probe_log")

# A late alert (e.g. memory-pressure noise after a container recreate) must not
# discard the measurement: record the post-run baseline and report it at the end.
if "$script_dir/moodle-environment-baseline.sh" verify >/dev/null 2>"$out_dir/baseline-after.err"; then
  baseline_after=passed
else
  baseline_after=failed
fi

diff_s() { awk -v a="$1" -v b="$2" 'BEGIN{printf "%.3f", b-a}'; }
jq -n \
  --arg run_id "$run_id" --arg scenario "$scenario" --arg alert "$expected_alert" \
  --argjson decision "$decision" \
  --arg inject "$(iso "${T[inject]}")" --arg detect "$(iso "${T[detect]}")" \
  --arg diagnose "$(iso "${T[diagnose]}")" --arg exec_start "$(iso "${T[execute_start]}")" \
  --arg exec_end "$(iso "${T[execute_end]}")" --arg recovered "$(iso "${T[recovered]}")" \
  --argjson mttd "$(diff_s "${T[inject]}" "${T[detect]}")" \
  --argjson diag "$(diff_s "${T[detect]}" "${T[diagnose]}")" \
  --argjson exec "$(diff_s "${T[execute_start]}" "${T[execute_end]}")" \
  --argjson rto_user "$(diff_s "${T[inject]}" "${T[recovered]}")" \
  --argjson rto_ops "$(diff_s "${T[detect]}" "${T[recovered]}")" \
  --argjson login_outage "$( [[ -n "$login_down" && -n "$login_up" ]] && diff_s "$login_down" "$login_up" || echo null)" \
  --argjson acked "$acked" --argjson lost "$lost" --argjson rpo "$rpo" \
  --argjson write_outage "${write_outage:-null}" --argjson stable "$stable_samples" \
  --argjson write_interval "$write_interval" --argjson probe_interval "$probe_interval" \
  --arg baseline_after "$baseline_after" \
  '{run_id:$run_id, scenario_id:$scenario, arm:"agent_without_gate_executor_verifier",
    baseline_after_run:$baseline_after,
    evidence_class:"staging_runtime_ablation", execution_authority:"allowlisted_test_harness_direct",
    safety_gate:"bypassed", operator_signature:"none", safe_executor:"not_used", independent_verifier:"not_used",
    resolution_basis:("\($stable) consecutive authenticated synthetic transactions"),
    alert:$alert, agent_decision:$decision,
    timestamps:{t_inject:$inject, t_detect:$detect, t_diagnose:$diagnose, t_execute_start:$exec_start,
                t_execute_end:$exec_end, t_recovered:$recovered},
    seconds:{mttd:$mttd, detect_to_diagnosis:$diag, execution:$exec,
             rto_user:$rto_user, rto_ops:$rto_ops, login_page_outage:$login_outage},
    rpo:{heartbeat_interval_s:$write_interval, acknowledged_writes:$acked, lost_acknowledged_writes:$lost,
         rpo_seconds:$rpo, write_unavailability_s:$write_outage},
    probe_interval_s:$probe_interval}' >"$out_dir/result.json"
chmod 0600 "$out_dir"/*

jq -r '"\n== \(.scenario_id) | \(.arm) ==",
  "MTTD (inject→alert activeAt) : \(.seconds.mttd) s",
  "Alert → Agent proposal       : \(.seconds.detect_to_diagnosis) s",
  "Direct execution             : \(.seconds.execution) s",
  "RTO user (inject→recovered)  : \(.seconds.rto_user) s",
  "RTO ops  (detect→recovered)  : \(.seconds.rto_ops) s",
  "Login page outage (probe)    : \(.seconds.login_page_outage) s",
  "RPO                          : \(.rpo.rpo_seconds) s (\(.rpo.lost_acknowledged_writes)/\(.rpo.acknowledged_writes) acknowledged writes lost)",
  "Write unavailability         : \(.rpo.write_unavailability_s) s",
  "Baseline after run           : \(.baseline_after_run)"' "$out_dir/result.json"
echo "evidence: $out_dir/result.json"
if [[ "$baseline_after" != passed ]]; then
  echo "Baseline is not clean after the run; see $out_dir/baseline-after.err before the next run." >&2
  exit 3
fi
