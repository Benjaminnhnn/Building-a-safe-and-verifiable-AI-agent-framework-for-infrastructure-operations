#!/usr/bin/env bash
set -euo pipefail

scenario="${1:-}"
case "$scenario" in ERP-01|ERP-02|ERP-03) ;; *) echo "Usage: $0 ERP-01|ERP-02|ERP-03" >&2; exit 64 ;; esac
[[ "${ERPNEXT_FAULT_CONFIRM:-}" == staging ]] || { echo "Set ERPNEXT_FAULT_CONFIRM=staging after confirming this is disposable staging." >&2; exit 77; }
[[ -r /etc/erpnext-staging-host && "$(cat /etc/erpnext-staging-host)" == erpnext-staging-01 ]] || { echo "Refusing fault outside the marked ERPNext staging host." >&2; exit 77; }
root=/opt/erpnext
compose=(docker compose --project-name erpnext -f "$root/docker-compose.yml")
test -f "$root/secrets/db_root_password" && test -f "$root/secrets/erpnext_admin_password"

case "$scenario" in
  ERP-01) "${compose[@]}" stop erpnext_mariadb ;;
  ERP-02) "${compose[@]}" stop erpnext_redis ;;
  ERP-03)
    test ! -e /var/lib/erpnext-faults/ERP-03.env || { echo "ERP-03 already has a saved baseline." >&2; exit 77; }
    install -d -o root -g root -m 0700 /var/lib/erpnext-faults
    cp -p "$root/.env" /var/lib/erpnext-faults/ERP-03.env
    ERPNEXT_BACKEND=erpnext-invalid-upstream:8000 "${compose[@]}" up -d --no-deps --force-recreate erpnext_nginx
    ;;
esac
printf '%s injected at %s\n' "$scenario" "$(date -u +%FT%TZ)"
