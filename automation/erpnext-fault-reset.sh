#!/usr/bin/env bash
set -euo pipefail

scenario="${1:-}"
case "$scenario" in ERP-01|ERP-02|ERP-03) ;; *) echo "Usage: $0 ERP-01|ERP-02|ERP-03" >&2; exit 64 ;; esac
[[ "${ERPNEXT_FAULT_CONFIRM:-}" == staging ]] || { echo "Set ERPNEXT_FAULT_CONFIRM=staging after confirming this is disposable staging." >&2; exit 77; }
[[ -r /etc/erpnext-staging-host && "$(cat /etc/erpnext-staging-host)" == erpnext-staging-01 ]] || { echo "Refusing reset outside the marked ERPNext staging host." >&2; exit 77; }
root=/opt/erpnext
compose=(docker compose --project-name erpnext -f "$root/docker-compose.yml")

case "$scenario" in
  ERP-01) "${compose[@]}" start erpnext_mariadb ;;
  ERP-02) "${compose[@]}" start erpnext_redis ;;
  ERP-03)
    backup=/var/lib/erpnext-faults/ERP-03.env
    test -r "$backup" || { echo "No owned ERP-03 baseline backup; refusing reset." >&2; exit 77; }
    expected=$(sha256sum "$backup" | awk '{print $1}')
    install -o root -g root -m 0640 "$backup" "$root/.env"
    actual=$(sha256sum "$root/.env" | awk '{print $1}')
    test "$actual" = "$expected"
    rm -f -- "$backup"
    "${compose[@]}" up -d --no-deps --force-recreate erpnext_nginx
    ;;
esac
printf '%s reset at %s\n' "$scenario" "$(date -u +%FT%TZ)"
