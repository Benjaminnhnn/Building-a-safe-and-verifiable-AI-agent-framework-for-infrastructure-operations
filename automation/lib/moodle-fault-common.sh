#!/usr/bin/env bash
# Shared, non-executable helpers for scoped Moodle staging fault drills.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
terraform_dir="$repo_root/terraform"
artifacts_dir="$terraform_dir/.artifacts"
ssh_config="$artifacts_dir/moodle-ssh.config"
fault_artifacts="$artifacts_dir/moodle-faults"
hosts=(moodle-app-a moodle-app-b)

require_commands() {
  local command_name
  for command_name in aws curl jq ssh terraform; do
    command -v "$command_name" >/dev/null 2>&1 || {
      echo "Required command not found: $command_name" >&2
      exit 127
    }
  done
}

load_moodle_environment() {
  require_commands
  [[ -r "$ssh_config" ]] || {
    echo "Missing SSH configuration: $ssh_config" >&2
    exit 66
  }
  application_json="$(terraform -chdir="$terraform_dir" output -json moodle_application)"
  database_json="$(terraform -chdir="$terraform_dir" output -json moodle_database)"
  filesystem_json="$(terraform -chdir="$terraform_dir" output -json moodle_filesystem)"
  public_url="$(jq -r '.url' <<<"$application_json")"
  database_endpoint="$(jq -r '.endpoint' <<<"$database_json")"
  target_group_arn="$(jq -r '.target_group_arn' <<<"$application_json")"
  region="$(jq -r '.alb_arn | split(":")[3]' <<<"$application_json")"
  moodledata_path="$(jq -r '.host_mount_path' <<<"$filesystem_json")"
  [[ "$public_url" == http://moodle-staging-* || "$public_url" == https://moodle-staging-* ]] || {
    echo "Refusing fault operation outside the Moodle staging URL: $public_url" >&2
    exit 77
  }
  [[ "$(jq -r '.legacy_demo_enabled' <<<"$application_json")" == false ]] || {
    echo "Refusing fault operation while the legacy demo is enabled." >&2
    exit 77
  }
  umask 077
  mkdir -p "$fault_artifacts"
}

validate_scenario() {
  case "${1:-}" in
    DB-01|DB-02|DB-03|RES-01|RES-02|RES-03|NET-01|NET-02|NET-03|CON-01|CON-02|CON-03|SEC-01|SEC-02|SEC-03) ;;
    *)
      echo "Scenario must be one of the 15 Moodle scenarios (DB-01..03, RES-01..03, NET-01..03, CON-01..03, SEC-01..03)" >&2
      exit 64
      ;;
  esac
}

remote() {
  local host="$1"
  shift
  ssh -F "$ssh_config" "$host" "$@"
}

compose_up_web() {
  local host="$1" recreate="${2:-false}" recreate_flag=""
  [[ "$recreate" == true ]] && recreate_flag="--force-recreate"
  remote "$host" "sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env -f /opt/moodle/release/docker-compose.yml up -d --wait $recreate_flag moodle-web"
}

record_fault_event() {
  local scenario="$1" action="$2" result="$3" details="${4:-}"
  jq -n \
    --arg scenario "$scenario" \
    --arg action "$action" \
    --arg result "$result" \
    --arg details "$details" \
    --arg observed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{scenario_id:$scenario,action:$action,result:$result,details:$details,observed_at:$observed_at}' \
    >> "$fault_artifacts/events.jsonl"
}

wait_for_alb_healthy() {
  local expected="${1:-2}" states
  for _ in {1..30}; do
    states="$(aws elbv2 describe-target-health --profile "${AWS_PROFILE:-target-account}" --region "$region" --target-group-arn "$target_group_arn" --query 'TargetHealthDescriptions[?TargetHealth.State==`healthy`]' --output json | jq length)"
    [[ "$states" -eq "$expected" ]] && return 0
    sleep 5
  done
  echo "ALB did not reach $expected healthy targets." >&2
  return 1
}

run_synthetic_once() {
  remote monitor-ai-01 "sudo /usr/local/sbin/moodle-synthetic-transaction --once --url '$public_url' --username admin --password-file /opt/moodle-observability/secrets/moodle-synthetic-password --output-dir /var/lib/moodle-synthetic/manual --metrics-file /var/lib/moodle-synthetic/moodle_synthetic.prom"
}

wait_for_prometheus_alert() {
  local alert_name="$1" timeout_seconds="${2:-120}" elapsed=0 state
  while (( elapsed < timeout_seconds )); do
    state="$(remote monitor-ai-01 "curl --fail --silent --get --data-urlencode 'query=ALERTS{alertname=\"$alert_name\",alertstate=\"firing\"}' http://127.0.0.1:9090/api/v1/query | jq -r 'if (.data.result | length) > 0 then \"firing\" else \"inactive\" end'")"
    [[ "$state" == firing ]] && return 0
    sleep 5
    elapsed=$((elapsed + 5))
  done
  echo "Prometheus alert did not fire within ${timeout_seconds}s: $alert_name" >&2
  return 1
}
