#!/usr/bin/env bash
# Single entrypoint for safe AI replay and live Moodle staging E2E drills.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  python_cmd=python3
elif command -v python >/dev/null 2>&1; then
  python_cmd=python
else
  echo "Required command not found: python3 or python" >&2
  exit 127
fi

usage() {
  cat <<'USAGE'
Usage:
  automation/run-aiops-e2e.sh replay [SCENARIO|all] [--output-dir DIR]
  AIOPS_E2E_LIVE_CONFIRM=staging automation/run-aiops-e2e.sh live <SCENARIO|all> [--runs 3] [--stability-seconds 120]
  AIOPS_E2E_LIVE_CONFIRM=staging automation/run-aiops-e2e.sh full <SCENARIO|all> [--runs 3] [--stability-seconds 120]

replay: runs the 15 merged AI benchmark contracts locally; no infrastructure mutation.
live:   runs one/all reviewed AWS Moodle staging faults and requires Alertmanager
        delivery plus unified-core shadow observation before controlled reset.
full:   runs all 15 local AI replays, then the requested live staging campaign.

Live scenarios: DB-01 RES-01 NET-01 CON-01 SEC-02 all
Replay scenarios: DB-01..03 RES-01..03 NET-01..03 CON-01..03 SEC-01..03 all
USAGE
}

mode="${1:-}"
[[ -n "$mode" ]] || { usage >&2; exit 64; }
shift

case "$mode" in
  replay)
    scenario="${1:-all}"
    (($# == 0)) || shift
    exec "$python_cmd" "$script_dir/aiops-unified-replay.py" "$scenario" "$@"
    ;;
  live|full)
    scenario="${1:-}"
    [[ -n "$scenario" ]] || { usage >&2; exit 64; }
    shift
    [[ "${AIOPS_E2E_LIVE_CONFIRM:-}" == staging ]] || {
      echo "Set AIOPS_E2E_LIVE_CONFIRM=staging to authorize scoped staging fault injection." >&2
      exit 77
    }
    if [[ "$mode" == full ]]; then
      "$python_cmd" "$script_dir/aiops-unified-replay.py" all
    fi
    exec "$script_dir/moodle-sprint2-live-drill.sh" "$scenario" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac
