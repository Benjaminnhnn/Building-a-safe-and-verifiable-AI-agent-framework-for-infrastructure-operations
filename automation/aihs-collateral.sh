#!/usr/bin/env bash
# Controlled SECOND fault for the AIHS supplementary stress test (docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md, section 16).
# Called only by the live harness, after the Safe Executor and after the health-only claim, inside the Verifier window.
#
#   bash automation/aihs-collateral.sh inject web_a_stop   # stop moodle-web on moodle-app-a (ALB keeps serving via B)
#   bash automation/aihs-collateral.sh reset  web_a_stop   # start it again and wait until both ALB targets are healthy
#
# Idempotent reset; it never touches moodle-app-b, the database, or any primary-scenario fixture.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

command="${1:-}"
kind="${2:-}"
[[ "${MOODLE_FAULT_CONFIRM:-}" == staging ]] || { echo "Set MOODLE_FAULT_CONFIRM=staging." >&2; exit 64; }
[[ "$kind" == web_a_stop ]] || { echo "Unknown collateral kind: $kind" >&2; exit 64; }
load_moodle_environment

case "$command" in
  inject)
    remote moodle-app-a "sudo docker stop release-moodle-web-1 >/dev/null && test \"\$(sudo docker inspect --format '{{.State.Running}}' release-moodle-web-1)\" = false"
    echo "collateral web_a_stop injected"
    ;;
  reset)
    compose_up_web moodle-app-a
    remote moodle-app-a "test \"\$(sudo docker inspect --format '{{.State.Running}}' release-moodle-web-1)\" = true"
    wait_for_alb_healthy 2
    echo "collateral web_a_stop reset: moodle-app-a web running, 2 healthy ALB targets"
    ;;
  *)
    echo "Usage: $0 inject|reset web_a_stop" >&2
    exit 64
    ;;
esac
