#!/usr/bin/env bash
# Idempotently remove one reviewed live Moodle staging fault.
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/moodle-fault-common.sh"

scenario="${1:-}"
validate_scenario "$scenario"
case "$scenario" in
  BIZ-01|BIZ-02|CON-01|DB-01|RES-01|NET-01|SEC-01) ;;
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

  RES-01)
    remote moodle-app-b "sudo docker rm -f moodle-fault-res-01 >/dev/null 2>&1 || true"
    ;;

  NET-01)
    for host in "${hosts[@]}"; do
      compose_up_web "$host" true
    done
    ;;

  CON-01)
    compose_up_web moodle-app-b
    ;;

  BIZ-01)
    remote moodle-app-a "
      set -eu
      sudo docker exec -u 1000:1999 release-moodle-web-1 php /var/www/html/admin/cli/maintenance.php --disable >/dev/null
      sudo test ! -e '$moodledata_path/climaintenance.html'
    "
    wait_for_prometheus_query 'max(moodle_cli_maintenance_mode_enabled{job="moodle_node"}) == 0' 120
    ;;

  BIZ-02)
    remote moodle-app-a "sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env -f /opt/moodle/release/docker-compose.yml --profile cron up -d --wait moodle-cron"
    # Stopping cron (inject) can kill it in the middle of another ad-hoc task; that task keeps its "running" claim
    # (timestarted/hostname/pid) and is never retried, so the queue stays overdue (16/11/2026, seq 2). Only cron runs
    # ad-hoc tasks, so any claim older than the current cron container start is orphaned: release just those claims.
    remote moodle-app-a '
      set -eu
      started="$(date -u -d "$(sudo docker inspect --format "{{.State.StartedAt}}" release-moodle-cron-1)" +%s)"
      sudo docker exec -u 1000:1999 release-moodle-web-1 php -r '\''
        define("CLI_SCRIPT", true);
        require "/var/www/html/config.php";
        $before = (int) $argv[1];
        $ids = $DB->get_fieldset_select("task_adhoc", "id", "timestarted IS NOT NULL AND timestarted < :t", ["t" => $before]);
        foreach ($ids as $id) {
            $DB->update_record("task_adhoc", (object) ["id" => $id, "timestarted" => null, "hostname" => null, "pid" => null]);
        }
        echo "BIZ-02 reset: released ", count($ids), " orphaned ad-hoc task claim(s)\n";
      '\'' "$started"
    '
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
    # The human operator may already have revoked the recorded rule; revoking a
    # missing rule ID fails, so act only on IDs still present in the group.
    present_ids="$(aws ec2 describe-security-group-rules \
      --profile "${AWS_PROFILE:-target-account}" --region "$region" \
      --filters "Name=group-id,Values=$rds_security_group" \
      --query 'SecurityGroupRules[].SecurityGroupRuleId' --output text)"
    for rule_id in $(tr ' \t' '\n\n' <<<"$rule_ids" | grep -E '^sgr-[0-9a-f]+$' | sort -u); do
      grep -qwF -- "$rule_id" <<<"$present_ids" || continue
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
