# Moodle platform operations: maintenance flag and cron service

This runbook applies to `MoodleMaintenanceModeStuck` (BIZ-01) and
`MoodleAdhocTaskQueueStuck` (BIZ-02). Both are handled at the
infrastructure layer; the agent never edits Moodle configuration or
`mdl_*` tables.

## Evidence to collect

- For the maintenance flag, confirm `moodle_cli_maintenance_mode_enabled` is
  1 on both nodes (the flag is `climaintenance.html` in the shared EFS
  dataroot) and `moodle_upgrade_in_progress` is 0 on both nodes.
- Check that both web containers are running and the public probe passes:
  a green health check while students see the maintenance page is the
  expected symptom.
- For the task queue, compare `moodle_adhoc_tasks_overdue_count` with
  `moodle_cron_container_stopped` and `moodle_node_cron_running`. Cron runs
  as the `moodle-cron` Compose service on the cron node only.

## Interpretation and safety

- Never disable maintenance mode while `admin/cli/upgrade.php` is running or
  when the upgrade probe cannot read the process table (reported as 1).
  Escalate to a human operator instead.
- The reviewed BIZ-01 action is the Moodle CLI maintenance disable on the
  staging application scope, with enable as rollback.
- The reviewed BIZ-02 action restarts the `moodle-cron` container from the
  reviewed Compose release. Deleting, unlocking or editing rows in
  `mdl_task_adhoc` is forbidden.
- A backlog with cron running is not this fault: it points to slow or
  failing tasks and needs a human review of the task logs.
- Recovery requires the Independent Verifier: student course access with
  HTTP 200 for BIZ-01; cron active and zero overdue ad-hoc tasks for BIZ-02.
