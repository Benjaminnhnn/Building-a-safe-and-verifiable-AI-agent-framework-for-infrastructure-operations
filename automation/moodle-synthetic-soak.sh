#!/usr/bin/env bash
# Manage the evidence-producing two-hour authenticated Moodle soak.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

action="${1:-}"
case "$action" in start|status|collect|stop) ;; *) echo "Usage: $0 <start|status|collect|stop>" >&2; exit 64 ;; esac
load_moodle_environment
local_state="$artifacts_dir/moodle-synthetic-soak"
current_file="$local_state/current-run-id"
mkdir -p "$local_state"

case "$action" in
  start)
    if remote moodle-app-a "sudo systemctl is-active --quiet moodle-synthetic-soak.service"; then
      echo "A Moodle synthetic soak is already active." >&2
      exit 69
    fi
    run_id="soak-$(date -u +%Y%m%dT%H%M%SZ)"
    printf '%s\n' "$run_id" > "$current_file"
    chmod 0600 "$current_file"
    remote moodle-app-a "
      sudo install -d -o root -g root -m 0700 '/var/lib/moodle-synthetic/$run_id'
      sudo systemctl reset-failed moodle-synthetic-soak.service >/dev/null 2>&1 || true
      sudo systemd-run --unit=moodle-synthetic-soak \
        --property=User=root --property=Group=root --property=NoNewPrivileges=true \
        /usr/local/sbin/moodle-synthetic-transaction \
        --url '$public_url' --username admin \
        --password-file /opt/moodle/secrets/moodle-admin-password \
        --output-dir '/var/lib/moodle-synthetic/$run_id' \
        --duration-seconds 7200 --interval-seconds 30 >/dev/null
    "
    echo "Started $run_id on moodle-app-a; planned duration is 7200 seconds."
    ;;
  status)
    [[ -r "$current_file" ]] || { echo "No local soak run identifier is recorded." >&2; exit 66; }
    run_id="$(<"$current_file")"
    remote moodle-app-a "
      sudo systemctl show moodle-synthetic-soak.service --property=ActiveState,SubState,Result --no-pager
      if sudo test -r '/var/lib/moodle-synthetic/$run_id/state.json'; then sudo cat '/var/lib/moodle-synthetic/$run_id/state.json'; fi
    "
    ;;
  collect)
    [[ -r "$current_file" ]] || { echo "No local soak run identifier is recorded." >&2; exit 66; }
    run_id="$(<"$current_file")"
    run_dir="$local_state/$run_id"
    mkdir -p "$run_dir"
    remote moodle-app-a "sudo cat '/var/lib/moodle-synthetic/$run_id/state.json'" > "$run_dir/state.json"
    remote moodle-app-a "sudo cat '/var/lib/moodle-synthetic/$run_id/transactions.jsonl'" > "$run_dir/transactions.jsonl"
    chmod 0600 "$run_dir/state.json" "$run_dir/transactions.jsonl"
    jq empty "$run_dir/state.json"
    jq -e -s 'length > 0 and all(.[]; has("success") and has("latency_seconds"))' "$run_dir/transactions.jsonl" >/dev/null
    echo "Collected $run_id evidence at $run_dir"
    ;;
  stop)
    remote moodle-app-a "sudo systemctl stop moodle-synthetic-soak.service || true"
    echo "Synthetic soak stopped. Partial evidence can still be collected."
    ;;
esac
