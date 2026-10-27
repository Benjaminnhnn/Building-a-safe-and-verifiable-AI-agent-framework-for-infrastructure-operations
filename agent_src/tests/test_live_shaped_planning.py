"""Diagnosis and planning on evidence shaped like the live collector output.

The 7-scenario suite overrides the diagnosis evidence refs and lists only the
queries each planner rule reads. These tests instead build one sample for every
query the collector issues for the alert, with the values the node probes and
blackbox exporter actually publish, and pass the diagnosis through unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.evidence.evidence_collectors import (
    BUSINESS_METRIC_QUERIES_BY_ALERT,
    METRIC_QUERIES_BY_ALERT,
    SECURITY_METRIC_QUERIES_BY_ALERT,
)
from core.moodle.diagnosis import diagnose_moodle_alert
from core.moodle.planner import plan_moodle_s3_3_remediation

A, B, MONITOR = "moodle-app-a", "moodle-app-b", "monitor-ai-01"

# The ad-hoc overdue count reads the shared database from every running web
# container, so both nodes report the same value; cron runs on node A only.
LIVE_SHAPES: dict[str, tuple[str, dict[str, dict[str, float]]]] = {
    "BIZ-01": ("MoodleMaintenanceModeStuck", {
        "moodle_cli_maintenance_mode_enabled": {A: 1, B: 1},
        "moodle_upgrade_in_progress": {A: 0, B: 0},
        "moodle_node_web_running": {A: 1, B: 1},
        "moodle_public_probe": {MONITOR: 1},
    }),
    "BIZ-02": ("MoodleAdhocTaskQueueStuck", {
        "moodle_adhoc_tasks_overdue_count": {A: 1, B: 1},
        "moodle_cron_container_stopped": {A: 1, B: 0},
        "moodle_node_web_running": {A: 1, B: 1},
    }),
    "CON-01": ("MoodleWebContainerMissing", {
        "moodle_web_container_last_seen_age_seconds": {A: 1},
        "moodle_fault_con01_web_stopped": {A: 0, B: 1},
        "moodle_node_web_running": {A: 1, B: 0},
        "moodle_node_up": {A: 1, B: 1},
        "moodle_cadvisor_up": {A: 1, B: 1},
    }),
    "DB-01": ("MoodleNodeRdsTcpFailed", {
        "moodle_node_rds_tcp_success": {A: 0, B: 0},
        "moodle_fault_db01_tagged_rule": {A: 1, B: 1},
    }),
    "RES-01": ("MoodleNodeCpuHigh", {
        "moodle_node_cpu_busy_percent": {A: 20, B: 95},
        "moodle_fault_res01_cpu_fixture_running": {A: 0, B: 1},
        "moodle_node_web_running": {A: 1, B: 1},
    }),
    "NET-01": ("MoodleSyntheticTransactionFailed", {
        "moodle_synthetic_success": {MONITOR: 0},
        "moodle_rds_tcp_probe": {MONITOR: 1},
        "moodle_public_probe": {MONITOR: 1},
        "moodle_efs_mount": {A: 1, B: 1},
        "moodle_fault_net01_hosts_override": {A: 1, B: 1},
        "moodle_fault_sec02_fixture_mode_zero": {A: 0, B: 0},
    }),
}


def _evidence(values: dict[str, dict[str, float]]) -> list[SimpleNamespace]:
    now = datetime.now(timezone.utc)
    return [
        SimpleNamespace(
            source="prometheus",
            evidence_id=f"ev-{query_id}-{instance}",
            collected_at=now,
            metadata={"payload_json": json.dumps({
                "query_id": query_id,
                "sample_value": float(value),
                "labels": {"instance": instance},
            })},
        )
        for query_id, per_instance in values.items()
        for instance, value in per_instance.items()
    ]


def _plan(scenario_id: str, alert_name: str, values: dict[str, dict[str, float]]):
    evidence = _evidence(values)
    diagnosis = diagnose_moodle_alert(alert_name, evidence)
    return diagnosis, plan_moodle_s3_3_remediation(
        incident_id=f"inc-{scenario_id}", alert_name=alert_name, diagnosis=diagnosis, evidence=evidence
    )


@pytest.mark.parametrize("scenario_id", sorted(LIVE_SHAPES))
def test_live_shaped_evidence_yields_the_scenario_candidate(scenario_id: str) -> None:
    alert_name, values = LIVE_SHAPES[scenario_id]
    queries = METRIC_QUERIES_BY_ALERT.get(alert_name) or BUSINESS_METRIC_QUERIES_BY_ALERT[alert_name]
    assert {query_id for query_id, _ in queries} == set(values)

    diagnosis, proposal = _plan(scenario_id, alert_name, values)

    assert diagnosis["status"] == "hypothesis"
    assert proposal is not None
    assert proposal["scenario_id"] == scenario_id
    assert set(diagnosis["evidence_refs"]) <= set(proposal["evidence_refs"])
    assert proposal["execution_permitted"] is False


def test_biz02_rejects_overdue_counts_that_disagree_between_nodes() -> None:
    alert_name, values = LIVE_SHAPES["BIZ-02"]
    _, proposal = _plan("BIZ-02", alert_name, {
        **values, "moodle_adhoc_tasks_overdue_count": {A: 0, B: 5},
    })
    assert proposal is None


def test_biz02_reason_does_not_claim_a_stopped_cron_that_is_running() -> None:
    alert_name, values = LIVE_SHAPES["BIZ-02"]
    diagnosis, proposal = _plan("BIZ-02", alert_name, {
        **values, "moodle_cron_container_stopped": {A: 0, B: 0},
    })
    assert "cron container" not in diagnosis["reason"]
    assert proposal is None


def test_biz01_needs_a_green_public_entry_probe() -> None:
    alert_name, values = LIVE_SHAPES["BIZ-01"]
    _, proposal = _plan("BIZ-01", alert_name, {**values, "moodle_public_probe": {MONITOR: 0}})
    assert proposal is None


def test_dependency_probes_are_one_series_attributed_to_the_monitor() -> None:
    tables = {**METRIC_QUERIES_BY_ALERT, **BUSINESS_METRIC_QUERIES_BY_ALERT, **SECURITY_METRIC_QUERIES_BY_ALERT}
    for alert_name in ("MoodleSyntheticTransactionFailed", "MoodleMaintenanceModeStuck", "MoodleDatabasePortExposure"):
        for query_id, expression in tables[alert_name]:
            if query_id not in {"moodle_public_probe", "moodle_rds_tcp_probe"}:
                continue
            assert expression.startswith("label_replace(probe_success{"), (alert_name, query_id)
            assert '"instance", "monitor-ai-01"' in expression
            if query_id == "moodle_public_probe":
                assert 'probe="healthz"' in expression
