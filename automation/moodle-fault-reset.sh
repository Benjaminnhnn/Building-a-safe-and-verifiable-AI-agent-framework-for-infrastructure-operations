#!/usr/bin/env bash
# Idempotently remove one allowlisted Moodle staging fault across all 15 scenarios.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
validate_scenario "$scenario"
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
    remote moodle-app-a "
      sudo docker rm -f moodle-fault-db-02 >/dev/null 2>&1 || true
    "
    ;;

  DB-03)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        if sudo test -r /var/lib/moodle-faults/DB-03-moodle-runtime.env.bak; then
          sudo cp /var/lib/moodle-faults/DB-03-moodle-runtime.env.bak /opt/moodle/release/moodle-runtime.env
          sudo rm -f /var/lib/moodle-faults/DB-03-moodle-runtime.env.bak
        fi
      "
      compose_up_web "$host" true
    done
    ;;

  RES-01)
    remote moodle-app-b "sudo docker rm -f moodle-fault-res-01 >/dev/null 2>&1 || true"
    ;;

  RES-02)
    remote moodle-app-b "sudo docker rm -f moodle-fault-res-02 >/dev/null 2>&1 || true"
    ;;

  RES-03)
    remote moodle-app-a "
      sudo rm -f '$moodledata_path/synthetic-fixtures/disk-fill-res-03.dat'
    "
    ;;

  NET-01)
    for host in "${hosts[@]}"; do
      compose_up_web "$host" true
    done
    ;;

  NET-02)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        while sudo iptables -C DOCKER-USER -p tcp --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT 2>/dev/null; do
          sudo iptables -D DOCKER-USER -p tcp --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT
        done
      "
    done
    ;;

  NET-03)
    remote moodle-app-a "
      set -eu
      sudo tc qdisc del dev eth0 root netem 2>/dev/null || true
    "
    ;;

  CON-01)
    compose_up_web moodle-app-b
    ;;

  CON-02)
    compose_up_web moodle-app-a
    ;;

  CON-03)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        if sudo test -r /var/lib/moodle-faults/CON-03-moodle-runtime.env.bak; then
          sudo cp /var/lib/moodle-faults/CON-03-moodle-runtime.env.bak /opt/moodle/release/moodle-runtime.env
          sudo rm -f /var/lib/moodle-faults/CON-03-moodle-runtime.env.bak
        fi
      "
      compose_up_web "$host" true
    done
    ;;

  SEC-01)
    remote moodle-app-a "
      set -eu
      while sudo iptables -C INPUT -p tcp --dport 5432 -m comment --comment moodle-fault-SEC-01 -j ACCEPT 2>/dev/null; do
        sudo iptables -D INPUT -p tcp --dport 5432 -m comment --comment moodle-fault-SEC-01 -j ACCEPT
      done
    "
    ;;

  SEC-02)
    remote moodle-app-a "sudo install -d -o 1000 -g 1000 -m 0770 '$moodledata_path/synthetic-fixtures' && sudo chown 1000:1000 '$moodledata_path/synthetic-fixtures' && sudo chmod 0770 '$moodledata_path/synthetic-fixtures'"
    ;;

  SEC-03)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        if sudo test -r /var/lib/moodle-faults/SEC-03-moodle-runtime.env.bak; then
          sudo cp /var/lib/moodle-faults/SEC-03-moodle-runtime.env.bak /opt/moodle/release/moodle-runtime.env
          sudo rm -f /var/lib/moodle-faults/SEC-03-moodle-runtime.env.bak
        fi
      "
      compose_up_web "$host" true
    done
    ;;
esac

wait_for_alb_healthy 2
record_fault_event "$scenario" reset passed "scoped staging fault removed"
echo "$scenario reset completed; both ALB targets are healthy."
