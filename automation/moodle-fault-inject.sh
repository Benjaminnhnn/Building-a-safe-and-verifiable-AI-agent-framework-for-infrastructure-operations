#!/usr/bin/env bash
# Inject one allowlisted, reversible Moodle staging fault across all 15 scenarios.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
validate_scenario "$scenario"
[[ "${MOODLE_FAULT_CONFIRM:-}" == staging ]] || {
  echo "Set MOODLE_FAULT_CONFIRM=staging to acknowledge the scoped fault." >&2
  exit 77
}
load_moodle_environment

case "$scenario" in
  DB-01)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        db_ip=\$(getent ahostsv4 '$database_endpoint' | awk 'NR==1 {print \$1}')
        test -n "\$db_ip"
        sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
        printf '%s\n' "\$db_ip" | sudo tee /var/lib/moodle-faults/DB-01.ip >/dev/null
        sudo chmod 0600 /var/lib/moodle-faults/DB-01.ip
        sudo iptables -S DOCKER-USER >/dev/null
        if ! sudo iptables -C DOCKER-USER -p tcp -d "\$db_ip" --dport 5432 -m comment --comment moodle-fault-DB-01 -j REJECT 2>/dev/null; then
          sudo iptables -I DOCKER-USER 1 -p tcp -d "\$db_ip" --dport 5432 -m comment --comment moodle-fault-DB-01 -j REJECT
        fi
        sudo iptables -C DOCKER-USER -p tcp -d "\$db_ip" --dport 5432 -m comment --comment moodle-fault-DB-01 -j REJECT
      "
    done
    ;;

  DB-02)
    remote moodle-app-a "
      sudo docker rm -f moodle-fault-db-02 >/dev/null 2>&1 || true
      sudo docker run -d --name moodle-fault-db-02 --restart=no alpine:3.20 sh -c 'sleep 3600' >/dev/null
      test "\$(sudo docker inspect --format '{{.State.Running}}' moodle-fault-db-02)" = true
    "
    ;;

  DB-03)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
        sudo cp /opt/moodle/release/moodle-runtime.env /var/lib/moodle-faults/DB-03-moodle-runtime.env.bak
        sudo sed -i 's/^MOODLE_DB_HOST=.*/MOODLE_DB_HOST=invalid-db-endpoint.internal/' /opt/moodle/release/moodle-runtime.env
      "
      compose_up_web "$host" true
    done
    ;;

  RES-01)
    remote moodle-app-b "
      sudo docker rm -f moodle-fault-res-01 >/dev/null 2>&1 || true
      sudo docker run -d --name moodle-fault-res-01 --restart=no --cpus=1.8 --memory=128m --pids-limit=64 alpine:3.20 sh -c 'for worker in 1 2; do yes >/dev/null & done; wait' >/dev/null
      test "\$(sudo docker inspect --format '{{.State.Running}}' moodle-fault-res-01)" = true
    "
    ;;

  RES-02)
    remote moodle-app-b "
      sudo docker rm -f moodle-fault-res-02 >/dev/null 2>&1 || true
      sudo docker run -d --name moodle-fault-res-02 --restart=no --memory=1024m alpine:3.20 sh -c 'tail -f /dev/null' >/dev/null
      test "\$(sudo docker inspect --format '{{.State.Running}}' moodle-fault-res-02)" = true
    "
    ;;

  RES-03)
    remote moodle-app-a "
      set -eu
      sudo install -d -o 1000 -g 1000 -m 0770 '$moodledata_path/synthetic-fixtures'
      sudo dd if=/dev/zero of='$moodledata_path/synthetic-fixtures/disk-fill-res-03.dat' bs=1M count=100 >/dev/null 2>&1
      test -f '$moodledata_path/synthetic-fixtures/disk-fill-res-03.dat'
    "
    ;;

  NET-01)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        if ! sudo docker exec -u 0 release-moodle-web-1 grep -Fq '# moodle-fault-NET-01' /etc/hosts; then
          printf '%s\n' '127.0.0.1 $database_endpoint # moodle-fault-NET-01' | sudo docker exec -i -u 0 release-moodle-web-1 sh -c 'cat >> /etc/hosts'
        fi
        sudo docker exec -u 0 release-moodle-web-1 grep -Fq '# moodle-fault-NET-01' /etc/hosts
      "
    done
    ;;

  NET-02)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        sudo iptables -S DOCKER-USER >/dev/null
        if ! sudo iptables -C DOCKER-USER -p tcp --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT 2>/dev/null; then
          sudo iptables -I DOCKER-USER 1 -p tcp --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT
        fi
        sudo iptables -C DOCKER-USER -p tcp --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT
      "
    done
    ;;

  NET-03)
    remote moodle-app-a "
      set -eu
      sudo tc qdisc add dev eth0 root netem delay 200ms loss 20% 2>/dev/null || sudo tc qdisc change dev eth0 root netem delay 200ms loss 20%
    "
    ;;

  CON-01)
    remote moodle-app-b "sudo docker stop release-moodle-web-1 >/dev/null && test "\$(sudo docker inspect --format '{{.State.Running}}' release-moodle-web-1)" = false"
    ;;

  CON-02)
    remote moodle-app-a "
      sudo docker stop release-moodle-web-1 >/dev/null 2>&1 || true
      test "\$(sudo docker inspect --format '{{.State.Running}}' release-moodle-web-1)" = false
    "
    ;;

  CON-03)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
        sudo cp /opt/moodle/release/moodle-runtime.env /var/lib/moodle-faults/CON-03-moodle-runtime.env.bak
        printf '%s\n' 'INVALID_SYNTAX_CRASH_DIRECTIVE=1' | sudo tee -a /opt/moodle/release/moodle-runtime.env >/dev/null
      "
    done
    ;;

  SEC-01)
    remote moodle-app-a "
      set -eu
      sudo iptables -I INPUT 1 -p tcp --dport 5432 -m comment --comment moodle-fault-SEC-01 -j ACCEPT
    "
    ;;

  SEC-02)
    remote moodle-app-a "
      set -eu
      sudo install -d -o 1000 -g 1000 -m 0770 '$moodledata_path/synthetic-fixtures'
      sudo chmod 0000 '$moodledata_path/synthetic-fixtures'
      test "\$(sudo stat -c '%a' '$moodledata_path/synthetic-fixtures')" = 0
    "
    ;;

  SEC-03)
    for host in "${hosts[@]}"; do
      remote "$host" "
        set -eu
        sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
        sudo cp /opt/moodle/release/moodle-runtime.env /var/lib/moodle-faults/SEC-03-moodle-runtime.env.bak
        sudo sed -i 's/^MOODLE_REVERSEPROXY=.*/MOODLE_REVERSEPROXY=0/' /opt/moodle/release/moodle-runtime.env
      "
      compose_up_web "$host" true
    done
    ;;
esac

record_fault_event "$scenario" inject passed "scoped staging fault installed"
echo "$scenario injected. Run automation/moodle-fault-reset.sh $scenario to restore the baseline."
