"""Reviewed live verification contracts accepted by the Agent runtime.

Covers both the Technical/Infrastructure scenarios (CON-01, DB-01, RES-01,
NET-01, SEC-02) and the Business/User-Admin workflow scenarios (BIZ-01..BIZ-04).
Human-only scenarios (SEC-01, BIZ-05) are intentionally excluded so they can
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
    # Business / User & Admin Usage Scenarios (BIZ-01 .. BIZ-04)
    # -----------------------------------------------------------------------
    "BIZ-01": {
        "allowed": ["common_synthetic_transaction", "verify_student_course_access"],
        "forbidden": ["internet_to_postgresql_blocked", "maintenance_banner_blocking_students"],
        "related": [
            "scenario_alert_resolved",
            "moodle_cli_maintenance_disabled",
            "student_course_view_http_200",
        ],
    },
    "BIZ-02": {
        "allowed": ["common_synthetic_transaction", "verify_moodle_task_queue_draining"],
        "forbidden": ["internet_to_postgresql_blocked", "adhoc_task_lock_stuck"],
        "related": [
            "scenario_alert_resolved",
            "overdue_adhoc_tasks_zero",
            "assignment_notification_dispatched",
        ],
    },
    "BIZ-03": {
        "allowed": ["common_synthetic_transaction", "verify_student_login_flow"],
        "forbidden": ["internet_to_postgresql_blocked", "student_auth_method_unavailable"],
        "related": [
            "scenario_alert_resolved",
            "approved_auth_plugins_enabled",
            "student_dashboard_session_established",
        ],
    },
    "BIZ-04": {
        "allowed": ["common_synthetic_transaction", "verify_course_self_enrolment_ready"],
        "forbidden": ["internet_to_postgresql_blocked", "self_enrolment_plugin_disabled"],
        "related": [
            "scenario_alert_resolved",
            "enrol_self_plugin_active",
            "student_course_enrolment_page_accessible",
        ],
    },
}

