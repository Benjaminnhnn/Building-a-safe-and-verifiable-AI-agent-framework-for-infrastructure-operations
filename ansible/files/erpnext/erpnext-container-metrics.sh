#!/usr/bin/env bash
set -euo pipefail

output_dir=/var/lib/node_exporter/textfile_collector
output_file="$output_dir/erpnext_containers.prom"
[[ -d "$output_dir" && ! -L "$output_dir" ]] || exit 1
metric() {
  local name="$1" container="$2" state=0
  if docker inspect "$container" >/dev/null 2>&1 && [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == true ]]; then
    state=1
  fi
  printf '%s %s\n' "$name" "$state"
}
temporary=$(mktemp "$output_dir/.erpnext_containers.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
{
  printf '# HELP erpnext_mariadb_up Whether the ERPNext MariaDB container is running.\n# TYPE erpnext_mariadb_up gauge\n'
  metric erpnext_mariadb_up erpnext_mariadb
  printf '# HELP erpnext_redis_up Whether the ERPNext Redis container is running.\n# TYPE erpnext_redis_up gauge\n'
  metric erpnext_redis_up erpnext_redis
  printf '# HELP erpnext_nginx_up Whether the ERPNext Nginx frontend container is running.\n# TYPE erpnext_nginx_up gauge\n'
  metric erpnext_nginx_up erpnext_nginx
} > "$temporary"
chmod 0644 "$temporary"
mv -f -- "$temporary" "$output_file"
