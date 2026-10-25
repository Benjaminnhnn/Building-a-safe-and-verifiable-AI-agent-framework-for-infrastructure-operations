#!/usr/bin/env bash
# Idempotently remove one reviewed live Moodle staging fault.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
validate_scenario "$scenario"
case "$scenario" in
  DB-01|DB-02|DB-03|RES-01|RES-02|RES-03|NET-01|NET-02|NET-03|CON-01|CON-02|CON-03|SEC-02|SEC-03|BIZ-01|BIZ-02|SEC-01) ;;
  *)
    echo "Refusing live reset for $scenario: its injector/reset pair has not been reviewed against the Moodle staging ground truth." >&2
    exit 77
    ;;
esac
load_moodle_environment

case "$scenario" in
  DB-01)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        if sudo test -r /var/lib/moodle-faults/DB-01.ip; then
          db_ip=\$(sudo cat /var/lib/moodle-faults/DB-01.ip)
        else
          db_ip=\$(getent ahostsv4 '$database_endpoint' | awk 'NR==1 {print \$1}')
        fi
        test -n "\$db_ip"
        while sudo iptables -C DOCKER-USER -p tcp -d "\$db_ip" --dport 5432 -m comment --comment moodle-fault-DB-01 -j REJECT 2>/dev/null; do
          sudo iptables -D DOCKER-USER -p tcp -d "\$db_ip" --dport 5432 -m comment --comment moodle-fault-DB-01 -j REJECT
        done
        sudo rm -f /var/lib/moodle-faults/DB-01.ip
      "
    done
    ;;

  DB-02)
    bash "$script_dir/moodle-db02-role-quota.sh" restore
    ;;

  DB-03)
    if remote moodle-app-b 'sudo test -r /var/lib/moodle-faults/DB-03.env'; then
      remote moodle-app-b '
        set -eu
        runtime=/opt/moodle/release/moodle-runtime.env
        backup=/var/lib/moodle-faults/DB-03.env
        expected=$(sudo sha256sum "$backup" | awk "{print \$1}")
        sudo cp -p -- "$backup" "$runtime"
        actual=$(sudo sha256sum "$runtime" | awk "{print \$1}")
        test "$actual" = "$expected" || { echo "DB-03 runtime restore checksum mismatch" >&2; exit 1; }
      '
      compose_up_web moodle-app-b true
      remote moodle-app-b 'sudo rm -f -- /var/lib/moodle-faults/DB-03.env'
    fi
    ;;

  RES-01)
    remote moodle-app-b "sudo docker rm -f moodle-fault-res-01 >/dev/null 2>&1 || true"
    ;;

  RES-02)
    remote moodle-app-b '
      set -eu
      fixture=/var/lib/moodle-faults/RES-02.memory
      if sudo test -e "$fixture"; then
        sudo docker rm -f moodle-fault-res-02 >/dev/null 2>&1 || true
        ! sudo docker ps -a --format "{{.Names}}" | grep -Fxq moodle-fault-res-02
        sudo rm -f -- "$fixture"
      elif sudo docker ps -a --format "{{.Names}}" | grep -Fxq moodle-fault-res-02; then
        echo "Refusing to delete unowned RES-02 named container" >&2
        exit 77
      fi
    '
    ;;

  RES-03)
    remote moodle-app-b '
      set -eu
      scratch=/var/lib/moodle-faults/res03-scratch
      if sudo test -d "$scratch"; then
        source=$(findmnt --noheadings --output SOURCE --mountpoint "$scratch" || true)
        fstype=$(findmnt --noheadings --output FSTYPE --mountpoint "$scratch" || true)
        if test -n "$source"; then
          test "$source" = tmpfs && test "$fstype" = tmpfs || {
            echo "Refusing to unmount a non-RES-03 filesystem" >&2
            exit 77
          }
          sudo umount -- "$scratch"
        fi
        sudo rmdir -- "$scratch"
      fi
    '
    ;;

  NET-01)
    for host in "${hosts[@]}"; do
      compose_up_web "$host" true
    done
    ;;

  NET-02)
    remote moodle-app-b '
      set -eu
      fixture=/var/lib/moodle-faults/NET-02.rule
      if sudo test -r "$fixture"; then
        set -- $(sudo cat "$fixture")
        test "$#" -eq 2 || { echo "NET-02 fixture is malformed" >&2; exit 77; }
        container_ip="$1"; db_ip="$2"
        while sudo iptables -C DOCKER-USER -s "$container_ip" -p tcp -d "$db_ip" --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT 2>/dev/null; do
          sudo iptables -D DOCKER-USER -s "$container_ip" -p tcp -d "$db_ip" --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT
        done
        sudo rm -f -- "$fixture"
      fi
      if sudo iptables -S DOCKER-USER | grep -Fq -- "--comment moodle-fault-NET-02"; then
        echo "NET-02 tagged rule remains after reset" >&2
        exit 77
      fi
    '
    ;;

  NET-03)
    remote moodle-app-b '
      set -eu
      fixture=/var/lib/moodle-faults/NET-03.netem
      if sudo test -r "$fixture"; then
        set -- $(sudo cat "$fixture")
        test "$#" -eq 4 || { echo "Malformed NET-03 fixture" >&2; exit 77; }
        old_id=$1; old_pid=$2; old_namespace=$3
        case "$old_id" in ""|*[!a-f0-9]*) echo "Malformed NET-03 container ID" >&2; exit 77 ;; esac
        case "$old_pid" in ""|*[!0-9]*) echo "Malformed NET-03 PID" >&2; exit 77 ;; esac
        case "$old_namespace" in net:\[*\]) ;; *) echo "Malformed NET-03 namespace" >&2; exit 77 ;; esac
        current_id=$(sudo docker inspect --format "{{.Id}}" release-moodle-web-1)
        current_pid=$(sudo docker inspect --format "{{.State.Pid}}" release-moodle-web-1)
        current_namespace=$(sudo readlink /proc/$current_pid/ns/net)
        if test "$current_id:$current_pid:$current_namespace" = "$old_id:$old_pid:$old_namespace"; then
          sudo nsenter -t "$current_pid" -n tc qdisc show dev eth0 | grep -Eq "^qdisc prio 1: root"
          sudo nsenter -t "$current_pid" -n tc qdisc show dev eth0 | grep -Eq "^qdisc netem 30: parent 1:3"
          sudo nsenter -t "$current_pid" -n tc qdisc del dev eth0 root handle 1:
        else
          echo "NET-03 container was replaced; checking its new namespace before clearing the stale fixture" >&2
        fi
        sudo nsenter -t "$current_pid" -n tc qdisc show dev eth0 | grep -Eq "^qdisc noqueue 0: root"
        sudo rm -f -- "$fixture"
      fi
    '
    ;;

  CON-01)
    compose_up_web moodle-app-b
    ;;

  CON-02)
    if ! remote moodle-app-b 'sudo docker exec release-moodle-web-1 a2query -c moodle-router >/dev/null'; then
      compose_up_web moodle-app-b true
    fi
    remote moodle-app-b 'sudo docker exec release-moodle-web-1 a2query -c moodle-router >/dev/null'
    ;;

  CON-03)
    if remote moodle-app-b 'sudo test -r /var/lib/moodle-faults/CON-03.env'; then
      remote moodle-app-b '
        set -eu
        runtime=/opt/moodle/release/moodle-runtime.env
        backup=/var/lib/moodle-faults/CON-03.env
        expected=$(sudo sha256sum "$backup" | awk "{print \$1}")
        sudo cp -p -- "$backup" "$runtime"
        actual=$(sudo sha256sum "$runtime" | awk "{print \$1}")
        test "$actual" = "$expected" || { echo "CON-03 runtime restore checksum mismatch" >&2; exit 1; }
      '
      compose_up_web moodle-app-b true
      remote moodle-app-b 'sudo rm -f -- /var/lib/moodle-faults/CON-03.env'
    fi
    ;;

  SEC-02)
    remote moodle-app-a "sudo install -d -o 1000 -g 1000 -m 0770 '$moodledata_path/synthetic-fixtures' && sudo chown 1000:1000 '$moodledata_path/synthetic-fixtures' && sudo chmod 0770 '$moodledata_path/synthetic-fixtures'"
    ;;

  SEC-03)
    if remote moodle-app-b 'sudo test -r /var/lib/moodle-faults/SEC-03.env'; then
      remote moodle-app-b '
        set -eu
        runtime=/opt/moodle/release/moodle-runtime.env
        backup=/var/lib/moodle-faults/SEC-03.env
        expected=$(sudo sha256sum "$backup" | awk "{print \$1}")
        sudo cp -p -- "$backup" "$runtime"
        actual=$(sudo sha256sum "$runtime" | awk "{print \$1}")
        test "$actual" = "$expected" || { echo "SEC-03 runtime restore checksum mismatch" >&2; exit 1; }
      '
      compose_up_web moodle-app-b true
      remote moodle-app-b 'sudo rm -f -- /var/lib/moodle-faults/SEC-03.env'
    fi
    ;;

  BIZ-01)
    remote moodle-app-a "
      set -eu
      sudo docker exec -u 1000 release-moodle-web-1 php /var/www/html/admin/cli/maintenance.php --disable >/dev/null
      sudo test ! -e '$moodledata_path/climaintenance.html'
    "
    wait_for_prometheus_query 'max(moodle_cli_maintenance_mode_enabled{job="moodle_node"}) == 0' 120
    ;;

  BIZ-02)
    remote moodle-app-a "sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env -f /opt/moodle/release/docker-compose.yml --profile cron up -d --wait moodle-cron"
    wait_for_prometheus_query 'max(moodle_adhoc_tasks_overdue_count{job="moodle_node"}) == 0' 300
    ;;

  SEC-01)
    load_sec01_network
    # Revoke the recorded rule and any orphan carrying the fault tag.
    rule_ids="$(aws ec2 describe-security-group-rules \
      --profile "${AWS_PROFILE:-target-account}" --region "$region" \
      --filters "Name=group-id,Values=$rds_security_group" 'Name=tag:moodle-fault,Values=SEC-01' \
      --query 'SecurityGroupRules[].SecurityGroupRuleId' --output text)"
    if [[ -r "$sec01_marker" ]]; then
      rule_ids="$rule_ids $(cat "$sec01_marker")"
    fi
    for rule_id in $(tr ' \t' '\n\n' <<<"$rule_ids" | grep -E '^sgr-[0-9a-f]+$' | sort -u); do
      aws ec2 revoke-security-group-ingress \
        --profile "${AWS_PROFILE:-target-account}" --region "$region" \
        --group-id "$rds_security_group" --security-group-rule-ids "$rule_id" >/dev/null
    done
    rm -f -- "$sec01_marker"
    wait_for_prometheus_query 'moodle_rds_sg_unreviewed_ingress_rules{job="moodle_synthetic",instance="monitor-ai-01"} == 0' 180
    ;;

esac

remove_scenario_marker "$scenario"
wait_for_alb_healthy 2
record_fault_event "$scenario" reset passed "scoped staging fault removed"
echo "$scenario reset completed; both ALB targets are healthy."
