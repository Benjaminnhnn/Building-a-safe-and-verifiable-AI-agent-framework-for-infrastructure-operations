#!/usr/bin/env bash
# Capture, verify, or reset the repeatable Moodle experiment baseline.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

action="${1:-}"
case "$action" in capture|verify|reset) ;; *) echo "Usage: $0 <capture|verify|reset>" >&2; exit 64 ;; esac
load_moodle_environment
baseline_dir="$artifacts_dir/moodle-experiment-baseline"
manifest="$baseline_dir/manifest.json"
mkdir -p "$baseline_dir"

runtime_snapshot() {
  local host="$1"
  remote "$host" '
    set -eu
    compose_sha=$(sudo sha256sum /opt/moodle/release/docker-compose.yml | awk "{print \$1}")
    env_sha=$(sudo sha256sum /opt/moodle/release/moodle-runtime.env | awk "{print \$1}")
    config_sha=$(sudo docker exec release-moodle-web-1 sha256sum /var/www/html/config.php | awk "{print \$1}")
    image=$(sudo docker inspect --format "{{.Config.Image}}" release-moodle-web-1)
    web_health=$(sudo docker inspect --format "{{.State.Health.Status}}" release-moodle-web-1)
    efs_type=$(findmnt --noheadings --output FSTYPE --target /mnt/efs/moodledata)
    jq -n --arg compose_sha "$compose_sha" --arg env_sha "$env_sha" --arg config_sha "$config_sha" --arg image "$image" --arg web_health "$web_health" --arg efs_type "$efs_type" "{compose_sha256:\$compose_sha,runtime_env_sha256:\$env_sha,config_sha256:\$config_sha,image:\$image,web_health:\$web_health,efs_type:\$efs_type}"
  '
}

capture() {
  local node_a node_b
  node_a="$(runtime_snapshot moodle-app-a)"
  node_b="$(runtime_snapshot moodle-app-b)"
  jq -n \
    --arg captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg public_url "$public_url" \
    --argjson node_a "$node_a" --argjson node_b "$node_b" \
    '{schema_version:"1.0",captured_at:$captured_at,environment:"staging",public_url:$public_url,nodes:{"moodle-app-a":$node_a,"moodle-app-b":$node_b}}' > "$manifest"
  chmod 0600 "$manifest"
  "$script_dir/capture-moodle-baseline.sh" >/dev/null
  echo "Experiment baseline captured at $manifest"
}

verify() {
  [[ -r "$manifest" ]] || { echo "Baseline manifest is missing; run capture first." >&2; exit 66; }
  local host current expected
  for host in "${hosts[@]}"; do
    current="$(runtime_snapshot "$host")"
    expected="$(jq -c --arg host "$host" '.nodes[$host]' "$manifest")"
    for field in compose_sha256 runtime_env_sha256 config_sha256 image; do
      [[ "$(jq -r --arg field "$field" '.[$field]' <<<"$current")" == "$(jq -r --arg field "$field" '.[$field]' <<<"$expected")" ]] || {
        echo "$host baseline mismatch: $field" >&2
        exit 1
      }
    done
    [[ "$(jq -r '.web_health' <<<"$current")" == healthy ]] || { echo "$host web container is not healthy" >&2; exit 1; }
    [[ "$(jq -r '.efs_type' <<<"$current")" =~ ^(nfs4|efs)$ ]] || { echo "$host EFS is not mounted" >&2; exit 1; }
  done
  wait_for_alb_healthy 2
  run_synthetic_once >/dev/null
  # The transaction runner writes a Node Exporter textfile. Wait until
  # Prometheus has scraped this *successful* result before declaring the
  # baseline clean; otherwise a prior failed sample can race a new drill.
  remote monitor-ai-01 '
    for attempt in $(seq 1 12); do
      value=$(curl --fail --silent --get --data-urlencode "query=moodle_synthetic_success{job=\"moodle_synthetic\",instance=\"monitor-ai-01\"}" http://127.0.0.1:9090/api/v1/query | jq -r ".data.result[0].value[1] // \"\"")
      test "$value" = 1 && exit 0
      sleep 5
    done
    echo "Prometheus did not scrape a successful Moodle synthetic result" >&2
    exit 1
  '
  remote moodle-app-a "sudo docker ps --format '{{.Names}}' | grep -qx release-moodle-cron-1"
  remote monitor-ai-01 "
    test \"\$(curl --fail --silent http://127.0.0.1:9090/api/v1/targets | jq '[.data.activeTargets[] | select(.health != \"up\")] | length')\" = 0
    for attempt in \$(seq 1 24); do
      alert_count=\$(curl --fail --silent http://127.0.0.1:9090/api/v1/alerts | jq '.data.alerts | length')
      test \"\$alert_count\" = 0 && exit 0
      sleep 5
    done
    exit 1
  "
  echo "Moodle experiment baseline verification passed."
}

reset() {
  local scenario
  for scenario in DB-01 RES-01 NET-01 CON-01 SEC-02; do
    "$script_dir/moodle-fault-reset.sh" "$scenario" >/dev/null
  done
  remote moodle-app-a "
    sudo docker exec -i release-moodle-web-1 php <<'PHP'
<?php
define('CLI_SCRIPT', true);
require '/var/www/html/config.php';
unset_all_config_for_plugin('local_synthetic');
PHP
    sudo find '$moodledata_path/synthetic-fixtures' -maxdepth 1 -type f -name 'fixture_*.txt' -delete
  "
  verify
  record_fault_event ALL reset passed "all known faults removed and fixture data cleaned"
}

"$action"
