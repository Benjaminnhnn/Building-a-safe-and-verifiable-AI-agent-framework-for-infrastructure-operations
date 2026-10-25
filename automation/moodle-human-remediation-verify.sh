#!/usr/bin/env bash
# Read-only verification after a HUMAN_ONLY remediation (SEC-01).
# moodle-scenario-verify.sh refuses SEC-01 so the agent path can never
# resolve it; this script checks the result of the manual security-group
# change and records the manual operator as the resolution actor.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/moodle-fault-common.sh"

scenario="${1:-}"
drill_id="${2:-}"
[[ "$scenario" == SEC-01 ]] || {
  echo "Only the human-only scenario SEC-01 is verified here." >&2
  exit 64
}
if [[ -n "$drill_id" && ! "$drill_id" =~ ^[A-Za-z0-9-]+$ ]]; then
  echo "Drill ID may contain only letters, digits, and hyphens." >&2
  exit 64
fi

load_moodle_environment
external_rds_health_check_id="$(terraform -chdir="$terraform_dir" output -json moodle_database | jq -r '.external_tcp_health_check_id // empty')"
checks='[]'

record_check() {
  local check_id="$1" description="$2" status="$3"
  checks="$(jq -c --arg id "$check_id" --arg description "$description" --arg status "$status" \
    '. + [{id:$id,description:$description,status:$status}]' <<<"$checks")"
}

prometheus_true() {
  remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=$1' http://127.0.0.1:9090/api/v1/query | jq -e '.data.result | length > 0' >/dev/null"
}

check() {
  local check_id="$1" description="$2" status=failed
  shift 2
  if "$@" >/dev/null 2>&1; then status=passed; fi
  record_check "$check_id" "$description" "$status"
}

check_internet_to_postgresql_blocked() {
  [[ -n "$external_rds_health_check_id" ]] || return 1
  AWS_PROFILE="${AWS_PROFILE:-target-account}" aws route53 get-health-check-status \
    --health-check-id "$external_rds_health_check_id" --output json | jq -e '
      .HealthCheckObservations as $observations
      | ($observations | length >= 3)
        and all($observations[]; .StatusReport.Status | startswith("Failure"))
    ' >/dev/null
}

check rds_ingress_audit_healthy \
  'The read-only RDS security-group audit succeeded within the last 5 minutes' \
  prometheus_true 'moodle_rds_sg_audit_success{job="moodle_synthetic",instance="monitor-ai-01"} == 1 and on() (time() - moodle_rds_sg_audit_timestamp_seconds{job="moodle_synthetic",instance="monitor-ai-01"} < 300)'
check rds_ingress_only_reviewed_sources \
  'The RDS security group has no unreviewed ingress source' \
  prometheus_true 'moodle_rds_sg_unreviewed_ingress_rules{job="moodle_synthetic",instance="monitor-ai-01"} == 0'
check common_synthetic_transaction \
  'Prometheus has a successful authenticated Moodle transaction newer than 180 seconds' \
  prometheus_true 'moodle_synthetic_success{job="moodle_synthetic",instance="monitor-ai-01"} == 1 and on() (time() - moodle_synthetic_last_run_timestamp_seconds{job="moodle_synthetic",instance="monitor-ai-01"} < 180)'
check internet_to_postgresql_blocked \
  'External Route 53 checkers fail to connect to PostgreSQL TCP 5432' \
  check_internet_to_postgresql_blocked

status=failed
if jq -e 'all(.[]; .status == "passed")' <<<"$checks" >/dev/null; then
  status=passed
fi
jq -cn --arg scenario_id "$scenario" --arg drill_id "$drill_id" --arg status "$status" \
  --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson checks "$checks" \
  '{scenario_id:$scenario_id,drill_id:(if $drill_id == "" then null else $drill_id end),status:$status,
    mode:"live_read_only",resolution_actor:"manual_operator",agent_resolution:false,
    checked_at:$checked_at,checks:$checks}'
[[ "$status" == passed ]]
