#!/usr/bin/env bash
# Read-only, node-local Moodle probes for the ALB/RDS/Apache fault campaign.
# Runs as a short systemd oneshot on each Moodle EC2; publishes no credentials.
set -euo pipefail

output_dir="${MOODLE_NODE_PROBE_OUTPUT_DIR:-/var/lib/node_exporter/textfile_collector}"
container="${MOODLE_NODE_PROBE_CONTAINER:-release-moodle-web-1}"
cron_container="${MOODLE_NODE_PROBE_CRON_CONTAINER:-release-moodle-cron-1}"
moodledata_path="${MOODLE_NODE_PROBE_MOODLEDATA:-/mnt/efs/moodledata}"
[[ "$output_dir" == /* && -d "$output_dir" && ! -L "$output_dir" ]] || {
  echo "Moodle probe output directory must be an existing, non-symlink absolute path." >&2
  exit 64
}
[[ "$container" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || exit 64
[[ "$cron_container" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || exit 64
[[ "$moodledata_path" == /* ]] || exit 64

web_running=0
web_exists=0
router_enabled=0
router_fallback_success=0
reverse_proxy_enabled=0
scratch_enospc=0
db_tcp_success=0
db_tcp_duration=0
restart_count=0
fault_db01_tagged_rule=0
fault_res01_cpu_fixture_running=0
fault_net01_hosts_override=0
fault_con01_web_stopped=0
fault_sec02_fixture_mode_zero=0
cli_maintenance_enabled=0
upgrade_in_progress=0
cron_running=0
cron_stopped=0
adhoc_probe_success=0
adhoc_overdue=""

if docker inspect "$container" >/dev/null 2>&1; then
  web_exists=1
  actual_running="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)"
  [[ "$actual_running" == true ]] && web_running=1
  actual_restarts="$(docker inspect --format '{{.RestartCount}}' "$container" 2>/dev/null || true)"
  [[ "$actual_restarts" =~ ^[0-9]+$ ]] && restart_count="$actual_restarts"
fi

# Exact, read-only facts for the five reviewed S3.3 remediations. Positive
# values require the fault's unique marker or exact fixture identity.
db_fault_ip="$(cat /var/lib/moodle-faults/DB-01.ip 2>/dev/null || true)"
if [[ "$db_fault_ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] &&
  iptables -C DOCKER-USER -p tcp -d "$db_fault_ip" --dport 5432 -m comment --comment moodle-fault-DB-01 -j REJECT 2>/dev/null; then
  fault_db01_tagged_rule=1
fi
if docker inspect moodle-fault-res-01 >/dev/null 2>&1 &&
  [[ "$(docker inspect --format '{{.State.Running}}' moodle-fault-res-01 2>/dev/null || true)" == true ]]; then
  fault_res01_cpu_fixture_running=1
fi
db_host="$(timeout 5 docker exec "$container" printenv MOODLE_DB_HOST 2>/dev/null || true)"
if (( web_running == 1 )) && [[ "$db_host" =~ ^[A-Za-z0-9.-]{1,253}$ ]] &&
  timeout 5 docker exec -u 0 "$container" grep -Fxq "127.0.0.1 $db_host # moodle-fault-NET-01" /etc/hosts 2>/dev/null; then
  fault_net01_hosts_override=1
fi
if (( web_exists == 1 && web_running == 0 )); then
  fault_con01_web_stopped=1
fi
fixture_path=/mnt/efs/moodledata/synthetic-fixtures
if [[ "$(findmnt --noheadings --output FSTYPE --target /mnt/efs/moodledata 2>/dev/null || true)" =~ ^(nfs4|efs)$ ]] &&
  [[ -d "$fixture_path" ]] &&
  [[ "$(stat -c '%u:%g:%a' "$fixture_path" 2>/dev/null || true)" == '1000:1000:0' ]]; then
  fault_sec02_fixture_mode_zero=1
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

# BIZ-01: the CLI maintenance flag is a file in the shared dataroot. Reading
# it is a filesystem fact; the probe never edits Moodle configuration.
if [[ -f "$moodledata_path/climaintenance.html" ]]; then
  cli_maintenance_enabled=1
fi

# BIZ-01 guard: an upgrade process in either Moodle container makes disabling
# maintenance unsafe. If the process table cannot be read, report 1 so the
# agent escalates instead of assuming the upgrade has finished.
for upgrade_container in "$container" "$cron_container"; do
  docker inspect "$upgrade_container" >/dev/null 2>&1 || continue
  [[ "$(docker inspect --format '{{.State.Running}}' "$upgrade_container" 2>/dev/null || true)" == true ]] || continue
  if ! process_table="$(timeout 5 docker top "$upgrade_container" -eo args 2>/dev/null)"; then
    upgrade_in_progress=1
  elif grep -Fq 'admin/cli/upgrade.php' <<<"$process_table"; then
    upgrade_in_progress=1
  fi
done

# BIZ-02: cron runs as a Compose service on the cron node only. A container
# that exists but is not running is the infrastructure-level fault.
if docker inspect "$cron_container" >/dev/null 2>&1; then
  if [[ "$(docker inspect --format '{{.State.Running}}' "$cron_container" 2>/dev/null || true)" == true ]]; then
    cron_running=1
  else
    cron_stopped=1
  fi
fi

# BIZ-02 symptom: read-only count of ad-hoc tasks overdue by 10 minutes. The
# count line is omitted on failure, so the agent sees missing evidence rather
# than a healthy zero.
if (( web_running == 1 )); then
  overdue="$(timeout 10 docker exec "$container" php -r '
    define("CLI_SCRIPT", true);
    require "/var/www/html/config.php";
    echo (int) $DB->count_records_select("task_adhoc", "nextruntime < :cutoff", ["cutoff" => time() - 600]);
  ' 2>/dev/null || true)"
  if [[ "$overdue" =~ ^[0-9]+$ ]]; then
    adhoc_probe_success=1
    adhoc_overdue="$overdue"
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
  '# HELP moodle_fault_db01_tagged_rule Whether the exact DB-01 fault-tagged rule is present.' \
  '# TYPE moodle_fault_db01_tagged_rule gauge' \
  "moodle_fault_db01_tagged_rule $fault_db01_tagged_rule" \
  '# HELP moodle_fault_res01_cpu_fixture_running Whether the exact RES-01 CPU fixture is running.' \
  '# TYPE moodle_fault_res01_cpu_fixture_running gauge' \
  "moodle_fault_res01_cpu_fixture_running $fault_res01_cpu_fixture_running" \
  '# HELP moodle_fault_net01_hosts_override Whether the exact NET-01 hosts override is present.' \
  '# TYPE moodle_fault_net01_hosts_override gauge' \
  "moodle_fault_net01_hosts_override $fault_net01_hosts_override" \
  '# HELP moodle_fault_con01_web_stopped Whether the existing reviewed Moodle web container is stopped.' \
  '# TYPE moodle_fault_con01_web_stopped gauge' \
  "moodle_fault_con01_web_stopped $fault_con01_web_stopped" \
  '# HELP moodle_fault_sec02_fixture_mode_zero Whether the exact synthetic fixture directory has the SEC-02 mode 0000.' \
  '# TYPE moodle_fault_sec02_fixture_mode_zero gauge' \
  "moodle_fault_sec02_fixture_mode_zero $fault_sec02_fixture_mode_zero" \
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
  '# HELP moodle_cli_maintenance_mode_enabled Whether climaintenance.html exists in the shared Moodle dataroot.' \
  '# TYPE moodle_cli_maintenance_mode_enabled gauge' \
  "moodle_cli_maintenance_mode_enabled $cli_maintenance_enabled" \
  '# HELP moodle_upgrade_in_progress Whether admin/cli/upgrade.php is running in a Moodle container (1 when unreadable).' \
  '# TYPE moodle_upgrade_in_progress gauge' \
  "moodle_upgrade_in_progress $upgrade_in_progress" \
  '# HELP moodle_node_cron_running Whether the Moodle cron container is running on this node.' \
  '# TYPE moodle_node_cron_running gauge' \
  "moodle_node_cron_running $cron_running" \
  '# HELP moodle_cron_container_stopped Whether the Moodle cron container exists on this node but is not running.' \
  '# TYPE moodle_cron_container_stopped gauge' \
  "moodle_cron_container_stopped $cron_stopped" \
  '# HELP moodle_adhoc_queue_probe_success Whether the read-only ad-hoc task backlog query succeeded.' \
  '# TYPE moodle_adhoc_queue_probe_success gauge' \
  "moodle_adhoc_queue_probe_success $adhoc_probe_success" \
  '# HELP moodle_node_probe_timestamp_seconds Unix timestamp of this local probe.' \
  '# TYPE moodle_node_probe_timestamp_seconds gauge' \
  "moodle_node_probe_timestamp_seconds $(date -u +%s)" \
  > "$temporary_file"
if (( adhoc_probe_success == 1 )); then
  printf '%s\n' \
    '# HELP moodle_adhoc_tasks_overdue_count Moodle ad-hoc tasks whose next run is more than 10 minutes overdue.' \
    '# TYPE moodle_adhoc_tasks_overdue_count gauge' \
    "moodle_adhoc_tasks_overdue_count $adhoc_overdue" >> "$temporary_file"
fi
chmod 0644 "$temporary_file"
mv -- "$temporary_file" "$output_dir/moodle_node_probes.prom"
trap - EXIT
