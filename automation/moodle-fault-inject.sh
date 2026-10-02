#!/usr/bin/env bash
# Inject one reviewed, reversible Moodle staging fault from the live allowlist.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
validate_scenario "$scenario"
case "$scenario" in
  DB-01|DB-02|DB-03|RES-01|RES-02|RES-03|NET-01|NET-02|NET-03|CON-01|CON-02|CON-03|SEC-02|SEC-03) ;;
  *)
    echo "Refusing live injection for $scenario: no reviewed injector/reset pair is available. Other scenarios remain offline/shadow-only." >&2
    exit 77
    ;;
esac
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
        test -n \"\$db_ip\"
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
    MOODLE_FAULT_CONFIRM=staging bash "$script_dir/moodle-db02-role-quota.sh" prepare
    php_code="$(cat <<'PHP'
$password = trim(file_get_contents(getenv('MOODLE_DB_PASSWORD_FILE')));
if ($password === '') exit(2);
putenv('PGPASSWORD=' . $password);
$connection = 'host=' . getenv('MOODLE_DB_HOST') . ' port=' . getenv('MOODLE_DB_PORT') . ' dbname=' . getenv('MOODLE_DB_NAME') . ' user=' . getenv('MOODLE_DB_USER') . ' sslmode=verify-full sslrootcert=/run/moodle-secrets/rds-ca.pem application_name=moodle-fault-DB-02 connect_timeout=5';
$database = @pg_connect($connection);
if ($database === false) exit(3);
@pg_query($database, 'SELECT pg_sleep(420)');
PHP
)"
    for _ in 1 2; do
      remote moodle-app-a "sudo docker exec -d release-moodle-web-1 php -r $(printf '%q' "$php_code")"
    done
    MOODLE_FAULT_CONFIRM=staging bash "$script_dir/moodle-db02-role-quota.sh" set-one
    ;;

  DB-03)
    remote moodle-app-b '
      set -eu
      runtime=/opt/moodle/release/moodle-runtime.env
      backup=/var/lib/moodle-faults/DB-03.env
      sudo test ! -e "$backup" || { echo "DB-03 backup already exists" >&2; exit 77; }
      test "$(sudo grep -c "^MOODLE_DB_HOST=" "$runtime")" -eq 1
      sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
      sudo cp -p -- "$runtime" "$backup"
      sudo sed -i "s/^MOODLE_DB_HOST=.*/MOODLE_DB_HOST=db03-invalid.invalid/" "$runtime"
      sudo grep -qx "MOODLE_DB_HOST=db03-invalid.invalid" "$runtime"
      sudo docker compose --env-file "$runtime" -f /opt/moodle/release/docker-compose.yml up -d --no-deps --force-recreate moodle-web >/dev/null
      sudo docker exec release-moodle-web-1 sh -c '\''test "$MOODLE_DB_HOST" = db03-invalid.invalid'\''
    '
    ;;

  RES-01)
    remote moodle-app-b "
      sudo docker rm -f moodle-fault-res-01 >/dev/null 2>&1 || true
      sudo docker run -d --name moodle-fault-res-01 --restart=no --cpus=1.8 --memory=128m --pids-limit=64 alpine:3.20 sh -c 'for worker in 1 2; do yes >/dev/null & done; wait' >/dev/null
      test \"\$(sudo docker inspect --format '{{.State.Running}}' moodle-fault-res-01)\" = true
    "
    ;;

  RES-02)
    remote moodle-app-b '
      set -eu
      fixture=/var/lib/moodle-faults/RES-02.memory
      sudo test ! -e "$fixture" || { echo "RES-02 fixture already exists" >&2; exit 77; }
      if sudo docker ps -a --format "{{.Names}}" | grep -Fxq moodle-fault-res-02; then
        echo "RES-02 named container already exists" >&2
        exit 77
      fi
      before=$(awk "/^MemAvailable:/ {print \$2}" /proc/meminfo)
      test "$before" -ge 786432 || { echo "Less than 768 MiB host memory available" >&2; exit 77; }
      sudo docker image inspect alpine:3.20 >/dev/null || { echo "RES-02 image not cached" >&2; exit 77; }
      sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
      printf "%s\n" "$before" | sudo tee "$fixture" >/dev/null
      sudo chmod 0600 "$fixture"
      sudo docker run -d --name moodle-fault-res-02 --restart=no \
        --memory=192m --memory-swap=192m --cpus=0.5 --pids-limit=32 \
        --tmpfs /pressure:rw,size=128m,mode=0700,nosuid,nodev,noexec \
        alpine:3.20 sh -c "dd if=/dev/zero of=/pressure/payload bs=1M count=96 2>/dev/null && sleep 900" >/dev/null
      for attempt in 1 2 3 4 5; do
        test "$(sudo docker inspect --format "{{.State.Running}}" moodle-fault-res-02)" = true || exit 1
        if sudo docker exec moodle-fault-res-02 test -s /pressure/payload; then break; fi
        sleep 2
      done
      test "$(sudo docker exec moodle-fault-res-02 stat -c %s /pressure/payload)" -eq 100663296
      after=$(awk "/^MemAvailable:/ {print \$2}" /proc/meminfo)
      test "$((before - after))" -ge 32768 || { echo "Host memory drop below 32 MiB" >&2; exit 1; }
      printf "%s\n" "$after" | sudo tee -a "$fixture" >/dev/null
    '
    ;;

  RES-03)
    remote moodle-app-b '
      set -eu
      scratch=/var/lib/moodle-faults/res03-scratch
      sudo test ! -e "$scratch" || { echo "RES-03 scratch path already exists" >&2; exit 77; }
      available_kib=$(awk "/^MemAvailable:/ {print \$2}" /proc/meminfo)
      test "$available_kib" -ge 524288 || { echo "Insufficient host memory headroom for 32 MiB tmpfs" >&2; exit 77; }
      sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
      sudo install -d -o root -g root -m 0700 "$scratch"
      sudo mount -t tmpfs -o size=32m,mode=0700,nosuid,nodev,noexec tmpfs "$scratch"
      test "$(findmnt --noheadings --output FSTYPE --mountpoint "$scratch")" = tmpfs
      if sudo dd if=/dev/zero of="$scratch/fill" bs=1M count=40 status=none 2>/dev/null; then
        echo "RES-03 tmpfs unexpectedly accepted more than its 32 MiB limit" >&2
        exit 1
      fi
      if sudo dd if=/dev/zero of="$scratch/probe" bs=1M count=1 status=none 2>/dev/null; then
        echo "RES-03 scratch write unexpectedly succeeded" >&2
        exit 1
      fi
      test "$(sudo df -Pk "$scratch" | awk "NR==2 {print \$4}")" -le 4
    '
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
    remote moodle-app-b "
      set -eu
      fixture=/var/lib/moodle-faults/NET-02.rule
      sudo test ! -e \"\$fixture\" || { echo 'NET-02 fixture already exists' >&2; exit 77; }
      if sudo iptables -S DOCKER-USER | grep -Fq -- '--comment moodle-fault-NET-02'; then
        echo 'NET-02 tagged rule already exists without this run owning it' >&2
        exit 77
      fi
      db_ip=\$(getent ahostsv4 '$database_endpoint' | awk 'NR==1 {print \$1}')
      container_ip=\$(sudo docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' release-moodle-web-1 | awk '{print \$1}')
      case \"\$db_ip:\$container_ip\" in *.*.*.*:*.*.*.*) ;; *) exit 77 ;; esac
      sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
      printf '%s %s\\n' \"\$container_ip\" \"\$db_ip\" | sudo tee \"\$fixture\" >/dev/null
      sudo chmod 0600 \"\$fixture\"
      sudo iptables -I DOCKER-USER 1 -s \"\$container_ip\" -p tcp -d \"\$db_ip\" --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT
      sudo iptables -C DOCKER-USER -s \"\$container_ip\" -p tcp -d \"\$db_ip\" --dport 5432 -m comment --comment moodle-fault-NET-02 -j REJECT
    "
    ;;

  NET-03)
    remote moodle-app-b "
      set -eu
      fixture=/var/lib/moodle-faults/NET-03.netem
      sudo test ! -e \"\$fixture\" || { echo 'NET-03 fixture already exists' >&2; exit 77; }
      command -v tc >/dev/null || { echo 'NET-03 requires iproute-tc via Ansible' >&2; exit 77; }
      container_id=\$(sudo docker inspect --format '{{.Id}}' release-moodle-web-1)
      pid=\$(sudo docker inspect --format '{{.State.Pid}}' release-moodle-web-1)
      test \"\$pid\" -gt 1
      namespace=\$(sudo readlink /proc/\$pid/ns/net)
      db_ip=\$(getent ahostsv4 '$database_endpoint' | awk 'NR==1 {print \$1}')
      case \"\$db_ip\" in *.*.*.*) ;; *) exit 77 ;; esac
      existing=\$(sudo nsenter -t \"\$pid\" -n tc qdisc show dev eth0)
      printf '%s\\n' \"\$existing\" | grep -Eq '^qdisc noqueue 0: root'
      test \"\$(printf '%s\\n' \"\$existing\" | wc -l)\" -eq 1 || { echo 'NET-03 unexpected preexisting qdisc' >&2; exit 77; }
      sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
      printf '%s %s %s %s\\n' \"\$container_id\" \"\$pid\" \"\$namespace\" \"\$db_ip\" | sudo tee \"\$fixture\" >/dev/null
      sudo chmod 0600 \"\$fixture\"
      sudo nsenter -t \"\$pid\" -n tc qdisc add dev eth0 root handle 1: prio bands 3
      sudo nsenter -t \"\$pid\" -n tc qdisc add dev eth0 parent 1:3 handle 30: netem delay 300ms 25ms loss 0.5%
      sudo nsenter -t \"\$pid\" -n tc filter add dev eth0 protocol ip parent 1: prio 1 u32 match ip dst \"\$db_ip\"/32 match ip dport 5432 0xffff flowid 1:3
      sudo nsenter -t \"\$pid\" -n tc qdisc show dev eth0 | grep -Eq '^qdisc netem 30: parent 1:3'
      sudo nsenter -t \"\$pid\" -n tc filter show dev eth0 parent 1: | grep -Fq 'flowid 1:3'
    "
    ;;

  CON-01)
    # Bind this controlled outage to its scenario before stopping the web
    # container so Prometheus can preserve scenario_id/drill_id on the alert.
    publish_scenario_marker "$scenario" "$(date -u +%Y%m%dT%H%M%SZ)"
    remote moodle-app-b "sudo docker stop release-moodle-web-1 >/dev/null && test \"\$(sudo docker inspect --format '{{.State.Running}}' release-moodle-web-1)\" = false"
    ;;

  CON-02)
    remote moodle-app-b '
      set -eu
      sudo docker exec release-moodle-web-1 a2query -c moodle-router >/dev/null
      sudo docker exec -u 0 release-moodle-web-1 a2disconf moodle-router >/dev/null
      sudo docker restart release-moodle-web-1 >/dev/null
      sudo docker exec -u 0 release-moodle-web-1 test ! -e /etc/apache2/conf-enabled/moodle-router.conf
    '
    ;;

  CON-03)
    remote moodle-app-b '
      set -eu
      runtime=/opt/moodle/release/moodle-runtime.env
      backup=/var/lib/moodle-faults/CON-03.env
      sudo test ! -e "$backup" || { echo "CON-03 backup already exists" >&2; exit 77; }
      test "$(sudo grep -c "^MOODLE_WWWROOT=" "$runtime")" -eq 1
      sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
      sudo cp -p -- "$runtime" "$backup"
      sudo sed -i "s#^MOODLE_WWWROOT=.*#MOODLE_WWWROOT=invalid-con03-host#" "$runtime"
      sudo grep -qx "MOODLE_WWWROOT=invalid-con03-host" "$runtime"
      sudo docker compose --env-file "$runtime" -f /opt/moodle/release/docker-compose.yml up -d --no-deps --force-recreate moodle-web >/dev/null
    '
    ;;

  SEC-02)
    remote moodle-app-a "
      set -eu
      sudo install -d -o 1000 -g 1000 -m 0770 '$moodledata_path/synthetic-fixtures'
      sudo chmod 0000 '$moodledata_path/synthetic-fixtures'
      test \"\$(sudo stat -c '%a' '$moodledata_path/synthetic-fixtures')\" = 0
    "
    ;;

  SEC-03)
    remote moodle-app-b '
      set -eu
      runtime=/opt/moodle/release/moodle-runtime.env
      backup=/var/lib/moodle-faults/SEC-03.env
      sudo test ! -e "$backup" || { echo "SEC-03 backup already exists" >&2; exit 77; }
      test "$(sudo grep -c "^MOODLE_REVERSE_PROXY=false$" "$runtime")" -eq 1
      sudo install -d -o root -g root -m 0700 /var/lib/moodle-faults
      sudo cp -p -- "$runtime" "$backup"
      sudo sed -i "s/^MOODLE_REVERSE_PROXY=false$/MOODLE_REVERSE_PROXY=true/" "$runtime"
      sudo grep -qx "MOODLE_REVERSE_PROXY=true" "$runtime"
      sudo docker compose --env-file "$runtime" -f /opt/moodle/release/docker-compose.yml up -d --no-deps --force-recreate moodle-web >/dev/null
      sudo docker exec release-moodle-web-1 sh -c '\''test "$MOODLE_REVERSE_PROXY" = true'\''
    '
    ;;

esac

record_fault_event "$scenario" inject passed "scoped staging fault installed"
echo "$scenario injected. Run automation/moodle-fault-reset.sh $scenario to restore the baseline."
