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
db_tcp_success=0
db_tcp_duration=0
restart_count=0
fault_db01_tagged_rule=0
fault_res01_cpu_fixture_running=0
fault_net01_hosts_override=0
fault_con01_web_stopped=0
cli_maintenance_enabled=0
upgrade_in_progress=0
cron_running=0
cron_stopped=0
adhoc_probe_success=0
adhoc_overdue=""
# AIHS schema alignment: same names/semantics as lab/moodle-local/node_probe.py.
# Optional values stay empty so the series is omitted (missing evidence, never a fake zero).
db_dns_success=0
db_dns_matches_expected=0
fixture_dir_exists=""
fixture_dir_mode=""
cron_heartbeat_age=""
cpu_percent=""

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

  # DNS answer of the DB alias inside the web container (honours /etc/hosts) versus the
  # approved endpoint resolved by the host resolver, which a container-scoped hosts
  # override cannot change. No credentials; a mismatch is a pre-decision observation.
  if [[ "$db_host" =~ ^[A-Za-z0-9.-]{1,253}$ ]]; then
    container_ip="$(timeout 7 docker exec "$container" php -r 'echo gethostbyname(getenv("MOODLE_DB_HOST"));' 2>/dev/null || true)"
    if [[ "$container_ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
      db_dns_success=1
      expected_ips="$(timeout 5 getent ahostsv4 "$db_host" 2>/dev/null | awk '{print $1}' | sort -u || true)"
      if [[ -n "$expected_ips" ]] && grep -Fxq -- "$container_ip" <<<"$expected_ips"; then
        db_dns_matches_expected=1
      fi
    fi
  fi

  # Synthetic fixture directory seen on the shared moodledata mount (same value on both
  # nodes). Mode is the numeric `stat %a` string (e.g. 770, 0), as in the lab probe.
  if [[ "$(findmnt --noheadings --output FSTYPE --target "$moodledata_path" 2>/dev/null || true)" =~ ^(nfs4|efs)$ ]]; then
    fixture_mode="$(stat -c '%a' "$moodledata_path/synthetic-fixtures" 2>/dev/null || true)"
    if [[ "$fixture_mode" =~ ^[0-7]{1,4}$ ]]; then
      fixture_dir_exists=1
      fixture_dir_mode=$((10#$fixture_mode))
    else
      fixture_dir_exists=0
    fi
  fi
fi

# Node CPU: sum of docker CPU% of this node's Moodle containers divided by vCPUs, capped at 100
# (same definition as the lab probe; node_exporter host CPU is deliberately not used).
node_vcpus="$(nproc 2>/dev/null || echo 1)"
[[ "$node_vcpus" =~ ^[1-9][0-9]*$ ]] || node_vcpus=1
if cpu_stats="$(timeout 15 docker stats --no-stream --format '{{.Name}} {{.CPUPerc}}' 2>/dev/null)" && [[ -n "$cpu_stats" ]]; then
  cpu_percent="$(awk -v vcpus="$node_vcpus" -v a="$container" -v b="$cron_container" -v c="moodle-fault-res-01" '
    $1 == a || $1 == b || $1 == c { v = $2; sub(/%$/, "", v); if (v ~ /^[0-9]+(\.[0-9]+)?$/) total += v }
    END { p = total / vcpus; if (p > 100) p = 100; printf "%.2f\n", p }' <<<"$cpu_stats")"
  [[ "$cpu_percent" =~ ^[0-9]+\.[0-9]{2}$ ]] || cpu_percent=""
fi

# BIZ-01: the CLI maintenance flag is a file in the shared dataroot. Reading
# it is a filesystem fact; the probe never edits Moodle configuration.
if [[ -f "$moodledata_path/climaintenance.html" ]]; then
  cli_maintenance_enabled=1
fi

# BIZ-01 guard: an upgrade process anywhere on the node (web, cron, or a
# one-off installer container) makes disabling maintenance unsafe. Container
# processes are visible in the host process table, which, unlike
# `docker top`, does not race with the cron container restarting while CLI
# maintenance is on. If the table cannot be read, report 1 so the agent
# escalates instead of assuming the upgrade has finished. Only a php process
# counts, so a grep for the path (e.g. this guard in the executor) does not.
if ! host_processes="$(ps -eo args 2>/dev/null)" || [[ -z "$host_processes" ]]; then
  upgrade_in_progress=1
elif awk '$1 ~ /(^|\/)php[0-9.]*$/ && /admin\/cli\/upgrade\.php/ { found = 1 } END { exit !found }' <<<"$host_processes"; then
  upgrade_in_progress=1
fi

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
    echo (int) $DB->count_records_select("task_adhoc", "nextruntime < :cutoff", ["cutoff" => time() - 600]), " ",
      (int) $DB->get_field("config", "value", ["name" => "lastcronstart"]);
  ' 2>/dev/null || true)"
  if [[ "$overdue" =~ ^([0-9]+)\ ([0-9]+)$ ]]; then
    adhoc_probe_success=1
    adhoc_overdue="${BASH_REMATCH[1]}"
    last_cron_start="${BASH_REMATCH[2]}"
    now_epoch="$(date -u +%s)"
    if (( last_cron_start > 0 )); then
      cron_heartbeat_age=$(( now_epoch > last_cron_start ? now_epoch - last_cron_start : 0 ))
    fi
  fi
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
  '# HELP moodle_node_apache_router_enabled Whether the Moodle Apache router config is enabled.' \
  '# TYPE moodle_node_apache_router_enabled gauge' \
  "moodle_node_apache_router_enabled $router_enabled" \
  '# HELP moodle_node_router_fallback_success Whether an unmapped route is handled by Moodle r.php rather than Apache.' \
  '# TYPE moodle_node_router_fallback_success gauge' \
  "moodle_node_router_fallback_success $router_fallback_success" \
  '# HELP moodle_node_reverse_proxy_enabled Whether Moodle reverse-proxy trust is enabled in the web container.' \
  '# TYPE moodle_node_reverse_proxy_enabled gauge' \
  "moodle_node_reverse_proxy_enabled $reverse_proxy_enabled" \
  '# HELP moodle_node_rds_tcp_success Whether the Moodle web container connected to RDS TCP 5432.' \
  '# TYPE moodle_node_rds_tcp_success gauge' \
  "moodle_node_rds_tcp_success $db_tcp_success" \
  '# HELP moodle_node_rds_tcp_duration_seconds TCP connect duration from the Moodle web container to RDS.' \
  '# TYPE moodle_node_rds_tcp_duration_seconds gauge' \
  "moodle_node_rds_tcp_duration_seconds $db_tcp_duration" \
  '# HELP moodle_node_db_dns_success Whether the DB alias resolved to an IPv4 address inside the web container.' \
  '# TYPE moodle_node_db_dns_success gauge' \
  "moodle_node_db_dns_success $db_dns_success" \
  '# HELP moodle_node_db_dns_matches_expected Whether the in-container DB alias answer equals the host-resolved approved endpoint.' \
  '# TYPE moodle_node_db_dns_matches_expected gauge' \
  "moodle_node_db_dns_matches_expected $db_dns_matches_expected" \
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
if [[ -n "$fixture_dir_exists" ]]; then
  printf '%s\n' \
    '# HELP moodle_node_fixture_dir_exists Whether the synthetic fixture directory exists on the moodledata mount.' \
    '# TYPE moodle_node_fixture_dir_exists gauge' \
    "moodle_node_fixture_dir_exists $fixture_dir_exists" >> "$temporary_file"
fi
if [[ -n "$fixture_dir_mode" ]]; then
  printf '%s\n' \
    '# HELP moodle_node_fixture_dir_mode Numeric stat %a of the synthetic fixture directory (octal digits read as decimal).' \
    '# TYPE moodle_node_fixture_dir_mode gauge' \
    "moodle_node_fixture_dir_mode $fixture_dir_mode" >> "$temporary_file"
fi
if [[ -n "$cron_heartbeat_age" ]]; then
  printf '%s\n' \
    '# HELP moodle_node_cron_heartbeat_age_seconds Seconds since Moodle config lastcronstart.' \
    '# TYPE moodle_node_cron_heartbeat_age_seconds gauge' \
    "moodle_node_cron_heartbeat_age_seconds $cron_heartbeat_age" >> "$temporary_file"
fi
if [[ -n "$cpu_percent" ]]; then
  printf '%s\n' \
    '# HELP moodle_node_cpu_percent Moodle container CPU percent of node vCPUs (docker stats, capped at 100).' \
    '# TYPE moodle_node_cpu_percent gauge' \
    "moodle_node_cpu_percent $cpu_percent" >> "$temporary_file"
fi
chmod 0644 "$temporary_file"
mv -- "$temporary_file" "$output_dir/moodle_node_probes.prom"
trap - EXIT
