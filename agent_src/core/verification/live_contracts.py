"""Reviewed live verification contracts accepted by the Agent runtime."""

MOODLE_LIVE_CONTRACTS = {
    "CON-01": {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": [
            "scenario_alert_resolved",
            "moodle_app_b_container_healthy",
            "both_alb_targets_healthy",
        ],
    },
}
