"""Reviewed live verification contracts accepted by the Agent runtime.

Covers both the Technical/Infrastructure scenarios (CON-01, DB-01, RES-01,
NET-01, SEC-02) and the Business/Infrastructure scenarios (BIZ-01, BIZ-02).
The human-only scenario SEC-01 is intentionally excluded so it can
never be auto-resolved by an agent execution receipt.
"""

MOODLE_LIVE_CONTRACTS: dict[str, dict[str, list[str]]] = {
    # -----------------------------------------------------------------------
    # Technical / Infrastructure Scenarios
    # -----------------------------------------------------------------------
    "CON-01": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "moodle_app_b_container_healthy",
            "both_alb_targets_healthy",
        ],
    },
    "DB-01": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "moodle-app-a_database_tls",
            "moodle-app-b_database_tls",
            "both_alb_targets_healthy",
        ],
    },
    "RES-01": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "named_cpu_fixture_absent",
        ],
    },
    "NET-01": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "moodle-app-a_database_dns",
            "moodle-app-b_database_dns",
            "both_alb_targets_healthy",
        ],
    },
    "SEC-02": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "fixture_directory_mode_restored",
            "efs_mount_metric_is_one",
        ],
    },
    # -----------------------------------------------------------------------
    # Business / Infrastructure Scenarios (BIZ-01, BIZ-02)
    # Check names must equal the ids automation/moodle-scenario-verify.sh
    # emits; the verifier rejects any coverage mismatch.
    # -----------------------------------------------------------------------
    "BIZ-01": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "moodle_cli_maintenance_disabled",
            "student_course_view_http_200",
        ],
    },
    "BIZ-02": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "moodle_cron_service_active",
            "overdue_adhoc_tasks_zero",
        ],
    },
}

