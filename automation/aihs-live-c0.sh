#!/usr/bin/env bash
# Operator wrapper for the LIVE AIHS C0-C3 staging harness (evaluation/aihs/live_c0_harness.py).
# Run from the repository root on the Operator host (WSL, ~/moodle-ops). Never in CI.
#
#   bash automation/aihs-live-c0.sh preflight                 # READY/BLOCKED (opens tunnels, no mutation)
#   bash automation/aihs-live-c0.sh plan OUT.json [REPS]      # randomised run plan (offline)
#   AIHS_LIVE_CONFIRM=staging bash automation/aihs-live-c0.sh run SCENARIO ARM SEQ OUTDIR
#   bash automation/aihs-live-c0.sh reset-scenario SCENARIO   # used by the harness; also safe to run by hand
#
# ARM: C0|C1 = shadow only (no live switch, nothing executed); C2|C3 = Operator-approved execution.
# The live switches (Agent + Safe Executor) are enabled ONLY for C2/C3 and always disabled again on exit.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"
source "$script_dir/lib/moodle-fault-common.sh"
PY="${AIHS_PYTHON:-python3}"
export PYTHONPATH="$repo_root/agent_src${PYTHONPATH:+:$PYTHONPATH}"
secrets_dir="$artifacts_dir/safe-executor"
tunnel_pids=()
agent_live=false
executor_live=false

cleanup() {
  local status=$?
  # A Ctrl-C / closed terminal must never stop the switch-off below (16/11/2026: an interrupted run left the
  # Safe Executor live switch ON). Ignore further signals while cleaning up.
  trap '' INT TERM HUP
  for pid in "${tunnel_pids[@]:-}"; do [[ -n "$pid" ]] && kill "$pid" >/dev/null 2>&1 || true; done
  if [[ "$executor_live" == true ]]; then SAFE_EXECUTOR_LIVE_ENABLED=false bash "$script_dir/configure-moodle-safe-executor-api.sh" >/dev/null || status=1; fi
  if [[ "$agent_live" == true ]]; then MOODLE_SAFE_ACTION_LIVE_ENABLED=false bash "$script_dir/configure-moodle-monitoring.sh" >/dev/null || status=1; fi
  [[ "$status" -eq 0 ]] || echo "aihs-live-c0 exited with status $status; verify the live switches are OFF and staging is at baseline." >&2
  return "$status"
}

open_tunnels() {
  ssh -F "$ssh_config" -o BatchMode=yes -o ExitOnForwardFailure=yes -N -L 127.0.0.1:19090:127.0.0.1:9090 monitor-ai-01 &
  tunnel_pids+=("$!")
  ssh -F "$ssh_config" -o BatchMode=yes -o ExitOnForwardFailure=yes -N -L 127.0.0.1:18000:127.0.0.1:8000 monitor-ai-01 &
  tunnel_pids+=("$!")
  sleep 3   # give ssh time to fail fast on a busy local port before probing
  for _ in {1..30}; do
    # A port already held by another process (e.g. the local lab's Prometheus on 19090) makes ssh exit while curl
    # still succeeds against the wrong backend: require our own tunnel processes to be alive.
    for pid in "${tunnel_pids[@]}"; do
      kill -0 "$pid" 2>/dev/null || { echo "Tunnel process $pid exited (local port in use?). Refusing to use another listener." >&2; return 1; }
    done
    curl --fail --silent --max-time 3 http://127.0.0.1:19090/-/ready >/dev/null && curl --fail --silent --max-time 3 http://127.0.0.1:18000/health >/dev/null && return 0
    sleep 1
  done
  echo "Cannot establish the Prometheus/Agent tunnels." >&2
  return 1
}

live_env() {
  load_moodle_environment
  export AIHS_LIVE_PUBLIC_URL="$public_url"
  export AIHS_LIVE_PROM_URL="${AIHS_LIVE_PROM_URL:-http://127.0.0.1:19090}"
  export AIHS_LIVE_AGENT_URL="${AIHS_LIVE_AGENT_URL:-http://127.0.0.1:18000}"
  export AIHS_LIVE_AGENT_TOKEN_FILE="${AIHS_LIVE_AGENT_TOKEN_FILE:-$secrets_dir/safe-action-api-token}"
  export AWS_PROFILE="${AWS_PROFILE:-target-account}"
}

cmd="${1:-}"
case "$cmd" in
  preflight)
    trap cleanup EXIT
    live_env
    open_tunnels
    "$PY" evaluation/aihs/live_c0_harness.py preflight
    ;;
  plan)
    "$PY" evaluation/aihs/live_c0_harness.py plan --out "${2:?plan output path}" --replicates "${3:-3}" --arms "${AIHS_ARMS:-C2,C3}"
    ;;
  reset-scenario)
    scenario="${2:?scenario}"
    [[ "${MOODLE_FAULT_CONFIRM:-}" == staging ]] || { echo "Set MOODLE_FAULT_CONFIRM=staging." >&2; exit 64; }
    load_moodle_environment
    "$script_dir/moodle-fault-reset.sh" "$scenario"
    remove_scenario_marker "$scenario"
    ;;
  run)
    scenario="${2:?scenario}"; arm="${3:?arm}"; seq="${4:-0}"; out="${5:?output dir}"
    [[ "${AIHS_LIVE_CONFIRM:-}" == staging ]] || { echo "Set AIHS_LIVE_CONFIRM=staging to authorise one live run." >&2; exit 77; }
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP     # turn a signal into a normal exit so the EXIT cleanup always runs
    live_env
    open_tunnels
    "$PY" evaluation/aihs/live_c0_harness.py preflight          # switches still OFF here
    if [[ "$arm" == C2 || "$arm" == C3 ]]; then
      executor_live=true
      SAFE_EXECUTOR_LIVE_ENABLED=true SAFE_EXECUTOR_LIVE_CONFIRM=staging bash "$script_dir/configure-moodle-safe-executor-api.sh" >/dev/null
      agent_live=true
      MOODLE_SAFE_ACTION_LIVE_ENABLED=true MOODLE_SAFE_ACTION_LIVE_CONFIRM=staging bash "$script_dir/configure-moodle-monitoring.sh" >/dev/null
    fi
    "$PY" evaluation/aihs/live_c0_harness.py run --scenario "$scenario" --arm "$arm" --seq "$seq" --out "$out" --skip-preflight \
      --approval "${AIHS_APPROVAL:-console}"
    ;;
  *)
    echo "Usage: $0 preflight | plan OUT.json [REPS] | run SCENARIO ARM SEQ OUTDIR | reset-scenario SCENARIO" >&2
    exit 64
    ;;
esac
