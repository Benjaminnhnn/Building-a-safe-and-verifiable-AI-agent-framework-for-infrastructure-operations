#!/usr/bin/env bash
# Manage the evidence-producing two-hour authenticated Moodle soak.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

action="${1:-}"
duration_seconds=7200
shift || true

if [[ "$action" == start && "${1:-}" == --duration-seconds ]]; then
  [[ $# -eq 2 && "$2" =~ ^[1-9][0-9]*$ ]] || {
    echo "Usage: $0 start [--duration-seconds <positive-integer>]" >&2
    exit 64
  }
  duration_seconds="$2"
  shift 2
fi

case "$action" in start|status|collect|stop) ;; *) echo "Usage: $0 <start|status|collect|stop>" >&2; exit 64 ;; esac
[[ $# -eq 0 ]] || { echo "Unexpected argument: $1" >&2; exit 64; }
load_moodle_environment
local_state="$artifacts_dir/moodle-synthetic-soak"
current_file="$local_state/current-run-id"
mkdir -p "$local_state"

case "$action" in
  start)
    if remote monitor-ai-01 "sudo systemctl is-active --quiet moodle-synthetic-soak.service"; then
      echo "A Moodle synthetic soak is already active." >&2
      exit 69
    fi
    run_id="soak-$(date -u +%Y%m%dT%H%M%SZ)"
    printf '%s\n' "$run_id" > "$current_file"
    chmod 0600 "$current_file"
    remote monitor-ai-01 "
      sudo install -d -o root -g root -m 0700 '/var/lib/moodle-synthetic/$run_id'
      sudo systemctl reset-failed moodle-synthetic-soak.service >/dev/null 2>&1 || true
      sudo systemd-run --unit=moodle-synthetic-soak \
        --property=User=root --property=Group=root --property=NoNewPrivileges=true \
        /usr/local/sbin/moodle-synthetic-transaction \
        --url '$public_url' --username admin \
        --password-file /opt/moodle-observability/secrets/moodle-synthetic-password \
        --output-dir '/var/lib/moodle-synthetic/$run_id' \
        --duration-seconds '$duration_seconds' --interval-seconds 30 >/dev/null
    "
    echo "Started $run_id on monitor-ai-01; planned duration is $duration_seconds seconds."
    ;;
  status)
    [[ -r "$current_file" ]] || { echo "No local soak run identifier is recorded." >&2; exit 66; }
    run_id="$(<"$current_file")"
    remote monitor-ai-01 "
      sudo systemctl show moodle-synthetic-soak.service --property=ActiveState,SubState,Result --no-pager
      if sudo test -r '/var/lib/moodle-synthetic/$run_id/state.json'; then sudo cat '/var/lib/moodle-synthetic/$run_id/state.json'; fi
    "
    ;;
  collect)
    [[ -r "$current_file" ]] || { echo "No local soak run identifier is recorded." >&2; exit 66; }
    run_id="$(<"$current_file")"
    run_dir="$local_state/$run_id"
    mkdir -p "$run_dir"
    remote monitor-ai-01 "sudo cat '/var/lib/moodle-synthetic/$run_id/state.json'" > "$run_dir/state.json"
    remote monitor-ai-01 "sudo cat '/var/lib/moodle-synthetic/$run_id/transactions.jsonl'" > "$run_dir/transactions.jsonl"
    chmod 0600 "$run_dir/state.json" "$run_dir/transactions.jsonl"
    jq empty "$run_dir/state.json"
    jq -e -s 'length > 0 and all(.[]; has("success") and has("latency_seconds"))' "$run_dir/transactions.jsonl" >/dev/null
    echo "Collected $run_id evidence at $run_dir"
    ;;
  stop)
    remote monitor-ai-01 "sudo systemctl stop moodle-synthetic-soak.service || true"
    echo "Synthetic soak stopped. Partial evidence can still be collected."
    ;;
esac
