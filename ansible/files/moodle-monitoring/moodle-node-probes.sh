#!/usr/bin/env bash
# Read-only, node-local Moodle probes for the ALB/RDS/Apache fault campaign.
# Runs as a short systemd oneshot on each Moodle EC2; publishes no credentials.
set -euo pipefail

output_dir="${MOODLE_NODE_PROBE_OUTPUT_DIR:-/var/lib/node_exporter/textfile_collector}"
container="${MOODLE_NODE_PROBE_CONTAINER:-release-moodle-web-1}"
[[ "$output_dir" == /* && -d "$output_dir" && ! -L "$output_dir" ]] || {
  echo "Moodle probe output directory must be an existing, non-symlink absolute path." >&2
  exit 64
}
[[ "$container" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || exit 64

web_running=0
router_enabled=0
router_fallback_success=0
reverse_proxy_enabled=0
scratch_enospc=0
db_tcp_success=0
db_tcp_duration=0
restart_count=0

if docker inspect "$container" >/dev/null 2>&1; then
  actual_running="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)"
  [[ "$actual_running" == true ]] && web_running=1
  actual_restarts="$(docker inspect --format '{{.RestartCount}}' "$container" 2>/dev/null || true)"
  [[ "$actual_restarts" =~ ^[0-9]+$ ]] && restart_count="$actual_restarts"
fi

if (( web_running == 1 )); then
  if timeout 5 docker exec "$container" test -e /etc/apache2/conf-enabled/moodle-router.conf >/dev/null 2>&1; then
    router_enabled=1
  fi
  if timeout 5 docker exec "$container" sh -c 'test "$MOODLE_REVERSE_PROXY" = true' >/dev/null 2>&1; then
    reverse_proxy_enabled=1
  fi

  # /course/view.php is a real file and succeeds even without FallbackResource.
  # A nonexistent path must reach Moodle's r.php router, which emits this
  # Moodle-specific 404 body; Apache's own 404 has a different body.
  if curl --max-time 5 --silent 'http://127.0.0.1:8080/__moodle_router_probe__/missing' 2>/dev/null |
    grep -Fq 'The requested resource could not be found. Please verify the URI'; then
    router_fallback_success=1
  fi

  # Probe from the web container network namespace, not the EC2 host. This
  # detects DOCKER-USER port rejects and container netem while the independent
  # monitor-to-RDS blackbox probe may remain healthy. No DB password is used.
  duration="$(timeout 7 docker exec "$container" php -r '
    $host = getenv("MOODLE_DB_HOST");
    if (!is_string($host) || !preg_match("/^[A-Za-z0-9.-]{1,253}$/", $host)) exit(2);
    $start = microtime(true);
    $socket = @fsockopen($host, 5432, $errno, $error, 3);
    if ($socket === false) exit(1);
    fclose($socket);
    printf("%.6f\n", microtime(true) - $start);
  ' 2>/dev/null || true)"
  if [[ "$duration" =~ ^[0-9]+\.[0-9]{6}$ ]]; then
    db_tcp_success=1
    db_tcp_duration="$duration"
  fi
fi

scratch_path=/var/lib/moodle-faults/res03-scratch
if [[ -d "$scratch_path" ]] && [[ "$(findmnt --noheadings --output FSTYPE --target "$scratch_path" 2>/dev/null || true)" == tmpfs ]]; then
  scratch_available="$(df -Pk "$scratch_path" 2>/dev/null | awk 'NR==2 {print $4}')"
  [[ "$scratch_available" =~ ^[0-9]+$ && "$scratch_available" -le 4 ]] && scratch_enospc=1
fi

temporary_file="$(mktemp "$output_dir/.moodle_node_probes.XXXXXX")"
cleanup() { [[ -e "$temporary_file" ]] && rm -f -- "$temporary_file"; }
trap cleanup EXIT
printf '%s\n' \
  '# HELP moodle_node_web_running Whether the named Moodle web container is running.' \
  '# TYPE moodle_node_web_running gauge' \
  "moodle_node_web_running $web_running" \
  '# HELP moodle_node_apache_router_enabled Whether the Moodle Apache router config is enabled.' \
  '# TYPE moodle_node_apache_router_enabled gauge' \
  "moodle_node_apache_router_enabled $router_enabled" \
  '# HELP moodle_node_router_fallback_success Whether an unmapped route is handled by Moodle r.php rather than Apache.' \
  '# TYPE moodle_node_router_fallback_success gauge' \
  "moodle_node_router_fallback_success $router_fallback_success" \
  '# HELP moodle_node_reverse_proxy_enabled Whether Moodle reverse-proxy trust is enabled in the web container.' \
  '# TYPE moodle_node_reverse_proxy_enabled gauge' \
  "moodle_node_reverse_proxy_enabled $reverse_proxy_enabled" \
  '# HELP moodle_node_scratch_enospc Whether the isolated RES-03 scratch tmpfs is full.' \
  '# TYPE moodle_node_scratch_enospc gauge' \
  "moodle_node_scratch_enospc $scratch_enospc" \
  '# HELP moodle_node_rds_tcp_success Whether the Moodle web container connected to RDS TCP 5432.' \
  '# TYPE moodle_node_rds_tcp_success gauge' \
  "moodle_node_rds_tcp_success $db_tcp_success" \
  '# HELP moodle_node_rds_tcp_duration_seconds TCP connect duration from the Moodle web container to RDS.' \
  '# TYPE moodle_node_rds_tcp_duration_seconds gauge' \
  "moodle_node_rds_tcp_duration_seconds $db_tcp_duration" \
  '# HELP moodle_node_web_restart_count Docker restart count for the Moodle web container.' \
  '# TYPE moodle_node_web_restart_count gauge' \
  "moodle_node_web_restart_count $restart_count" \
  '# HELP moodle_node_probe_timestamp_seconds Unix timestamp of this local probe.' \
  '# TYPE moodle_node_probe_timestamp_seconds gauge' \
  "moodle_node_probe_timestamp_seconds $(date -u +%s)" \
  > "$temporary_file"
chmod 0644 "$temporary_file"
mv -- "$temporary_file" "$output_dir/moodle_node_probes.prom"
trap - EXIT
