#!/usr/bin/env bash
# Inject one reviewed, reversible Moodle staging fault from the live allowlist.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
validate_scenario "$scenario"
case "$scenario" in
  BIZ-01|BIZ-02|CON-01|DB-01|RES-01|NET-01|SEC-01) ;;
  *)
    echo "Refusing live injection for $scenario: no reviewed injector/reset pair is available. Other scenarios remain offline/shadow-only." >&2
    exit 77
    ;;
esac
[[ "${MOODLE_FAULT_CONFIRM:-}" == staging ]] || {
  echo "Set MOODLE_FAULT_CONFIRM=staging to acknowledge the scoped fault." >&2
  exit 77
}
# SEC-01 changes a real AWS security group, so it needs its own acknowledgement.
if [[ "$scenario" == SEC-01 && "${MOODLE_SEC01_CONFIRM:-}" != rds-security-group ]]; then
  echo "Set MOODLE_SEC01_CONFIRM=rds-security-group to add the tagged SEC-01 RDS ingress rule." >&2
  exit 77
fi
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

  RES-01)
    remote moodle-app-b "
      sudo docker rm -f moodle-fault-res-01 >/dev/null 2>&1 || true
      sudo docker run -d --name moodle-fault-res-01 --restart=no --cpus=1.8 --memory=128m --pids-limit=64 alpine:3.20 sh -c 'for worker in 1 2; do yes >/dev/null & done; wait' >/dev/null
      test \"\$(sudo docker inspect --format '{{.State.Running}}' moodle-fault-res-01)\" = true
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

  CON-01)
    # Keep the controlled-outage identity in the restricted host marker only;
    # Prometheus receives the missing-container symptom without scenario IDs.
    publish_scenario_marker "$scenario" "$(date -u +%Y%m%dT%H%M%SZ)"
    remote moodle-app-b "sudo docker stop release-moodle-web-1 >/dev/null && test \"\$(sudo docker inspect --format '{{.State.Running}}' release-moodle-web-1)\" = false"
    ;;

  BIZ-01)
    publish_scenario_marker "$scenario" "$(date -u +%Y%m%dT%H%M%SZ)"
    remote moodle-app-a "
      set -eu
      sudo docker exec -u 1000:1999 release-moodle-web-1 php /var/www/html/admin/cli/maintenance.php --enable >/dev/null
      sudo test -f '$moodledata_path/climaintenance.html'
    "
    ;;

  BIZ-02)
    # Stop cron, then queue one benign core ad-hoc task with a next run time
    # in the past so it is overdue at once. refresh_mod_calendar_events_task
    # only regenerates assignment calendar events from existing data. The
    # class is checked first so a different Moodle version fails closed.
    publish_scenario_marker "$scenario" "$(date -u +%Y%m%dT%H%M%SZ)"
    remote moodle-app-a '
      set -eu
      sudo docker stop release-moodle-cron-1 >/dev/null
      test "$(sudo docker inspect --format "{{.State.Running}}" release-moodle-cron-1)" = false
      sudo docker exec -u 1000:1999 release-moodle-web-1 php -r '\''
        define("CLI_SCRIPT", true);
        require "/var/www/html/config.php";
        $class = "\\core\\task\\refresh_mod_calendar_events_task";
        if (!class_exists($class)) { fwrite(STDERR, "BIZ-02 fixture task class is missing\n"); exit(77); }
        $task = new $class();
        $task->set_custom_data(["plugins" => ["assign"], "moodle_fault" => "BIZ-02"]);
        $task->set_next_run_time(time() - 900);
        \core\task\manager::queue_adhoc_task($task);
      '\''
    '
    ;;

  SEC-01)
    # Per the SEC-01 ground truth the unauthorized source is the ALB security
    # group, not a public CIDR, so RDS stays private during the drill.
    load_sec01_network
    [[ ! -e "$sec01_marker" ]] || { echo "SEC-01 rule marker already exists: $sec01_marker" >&2; exit 77; }
    rule_id="$(aws ec2 authorize-security-group-ingress \
      --profile "${AWS_PROFILE:-target-account}" --region "$region" \
      --group-id "$rds_security_group" \
      --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$alb_security_group,Description=moodle-fault-SEC-01}]" \
      --tag-specifications 'ResourceType=security-group-rule,Tags=[{Key=moodle-fault,Value=SEC-01}]' \
      --query 'SecurityGroupRules[0].SecurityGroupRuleId' --output text)"
    [[ "$rule_id" =~ ^sgr-[0-9a-f]+$ ]] || { echo "SEC-01 rule was not created" >&2; exit 1; }
    printf '%s\n' "$rule_id" > "$sec01_marker"
    ;;

esac

record_fault_event "$scenario" inject passed "scoped staging fault installed"
echo "$scenario injected. Run automation/moodle-fault-reset.sh $scenario to restore the baseline."
