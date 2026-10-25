"""Monitoring-to-agent contract for the seven thesis scenarios.

Each scenario alert must exist as a Prometheus rule, map to an inventory
resource, have reviewed evidence queries, and every metric it queries must be
published by an exporter in this repository. Also covers SEC-01 diagnosis and
the administrator action report.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.moodle.adapter import MOODLE_ALERT_RESOURCE_MAP, MoodlePlatformAdapter
from core.moodle.diagnosis import diagnose_moodle_alert
from core.moodle.operator_report import build_admin_action_report
from core.moodle.planner import (
    describe_moodle_remediation_disposition,
    plan_moodle_s3_3_remediation,
)

REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "ansible" / "config" / "moodle_alert_rules.yml"
EXPORTER_SOURCES = (
    REPO / "ansible" / "files" / "moodle-monitoring" / "moodle-node-probes.sh",
    REPO / "ansible" / "files" / "moodle-monitoring" / "moodle-rds-ingress-audit.sh",
    REPO / "ansible" / "playbooks" / "configure-moodle-monitoring.yml",
    REPO / "automation" / "moodle-synthetic-transaction.py",
)
# Metrics produced by Prometheus itself, node exporter, cAdvisor or Blackbox.
STANDARD_METRICS = {"up", "probe_success", "node_cpu_seconds_total", "node_memory_MemAvailable_bytes", "container_last_seen"}

THESIS_SCENARIO_ALERTS = {
    "BIZ-01": "MoodleMaintenanceModeStuck",
    "BIZ-02": "MoodleAdhocTaskQueueStuck",
    "CON-01": "MoodleWebContainerMissing",
    "DB-01": "MoodleNodeRdsTcpFailed",
    "RES-01": "MoodleNodeCpuHigh",
    "NET-01": "MoodleSyntheticTransactionFailed",
    "SEC-01": "MoodleDatabasePortExposure",
}


def _rules() -> dict[str, dict]:
    data = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return {rule["alert"]: rule for group in data["groups"] for rule in group["rules"]}


def _metric_names(promql: str) -> set[str]:
    names = set(re.findall(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[|\)|$|\s)", promql))
    keywords = {"by", "on", "and", "or", "unless", "time", "avg", "max", "rate", "changes", "absent", "delta", "instance", "mode", "job", "name"}
    return {name for name in names if name not in keywords and not name.isdigit()}


@pytest.mark.parametrize("scenario_id,alert_name", sorted(THESIS_SCENARIO_ALERTS.items()))
def test_thesis_alert_is_a_staging_prometheus_rule(scenario_id: str, alert_name: str) -> None:
    rule = _rules().get(alert_name)
    assert rule is not None, f"{scenario_id} alert {alert_name} has no Prometheus rule"
    assert rule["labels"]["environment"] == "staging"
    assert rule["labels"]["service"] == "moodle"


@pytest.mark.parametrize("scenario_id,alert_name", sorted(THESIS_SCENARIO_ALERTS.items()))
def test_thesis_alert_reaches_agent_with_reviewed_queries(scenario_id: str, alert_name: str) -> None:
    adapter = MoodlePlatformAdapter()
    alert = {"labels": {"alertname": alert_name, "service": "moodle", "environment": "staging"}}
    assert adapter.identify_resource(alert) == MOODLE_ALERT_RESOURCE_MAP[alert_name]
    if alert_name != "MoodleSyntheticTransactionFailed":
        assert adapter.get_alert_queries(alert_name), f"{scenario_id} has no evidence queries"


def test_every_queried_and_alerted_metric_has_an_exporter() -> None:
    exported_text = "\n".join(path.read_text(encoding="utf-8") for path in EXPORTER_SOURCES)
    adapter = MoodlePlatformAdapter()
    expressions = [rule["expr"] for rule in _rules().values()]
    for alert_name in THESIS_SCENARIO_ALERTS.values():
        expressions.extend(promql for _, promql in adapter.get_alert_queries(alert_name))
    missing = sorted({
        name
        for expression in expressions
        for name in _metric_names(expression)
        if name not in STANDARD_METRICS and name not in exported_text
    })
    assert missing == []


def test_business_evidence_queries_use_existing_scrape_jobs() -> None:
    playbook = (REPO / "ansible" / "playbooks" / "configure-moodle-monitoring.yml").read_text(encoding="utf-8")
    jobs = set(re.findall(r"job_name:\s*([a-z_]+)", playbook))
    adapter = MoodlePlatformAdapter()
    for alert_name in THESIS_SCENARIO_ALERTS.values():
        for _, promql in adapter.get_alert_queries(alert_name):
            for job in re.findall(r'job="([a-z_]+)"', promql):
                assert job in jobs, f"{alert_name} queries unknown scrape job {job}"


def _metric(query_id: str, value: float, instance: str, suffix: str):
    return SimpleNamespace(
        source="prometheus",
        evidence_id=f"ev-{suffix}",
        collected_at=datetime.now(timezone.utc),
        metadata={"payload_json": json.dumps({"query_id": query_id, "sample_value": value, "labels": {"instance": instance}})},
    )


def _sec01_evidence(unreviewed: float, public: float) -> list[SimpleNamespace]:
    return [
        _metric("moodle_rds_sg_unreviewed_ingress_rules", unreviewed, "monitor-ai-01", "unreviewed"),
        _metric("moodle_rds_sg_public_ingress_rules", public, "monitor-ai-01", "public"),
        _metric("moodle_rds_tcp_probe", 1.0, "monitor-ai-01", "rds"),
    ]


def test_sec01_is_diagnosed_but_only_escalated_to_humans() -> None:
    evidence = _sec01_evidence(1.0, 1.0)
    diagnosis = diagnose_moodle_alert("MoodleDatabasePortExposure", evidence)
    assert diagnosis["status"] == "hypothesis"
    assert diagnosis["cause_code"] == "rds_ingress_unreviewed_source"
    assert diagnosis["confidence"] == pytest.approx(0.9)
    assert set(diagnosis["evidence_refs"]) == {"ev-unreviewed", "ev-public", "ev-rds"}

    candidate = plan_moodle_s3_3_remediation(
        incident_id="inc-sec01",
        alert_name="MoodleDatabasePortExposure",
        diagnosis=diagnosis,
        evidence=evidence,
    )
    assert candidate is None
    disposition = describe_moodle_remediation_disposition(
        alert_name="MoodleDatabasePortExposure", diagnosis=diagnosis
    )
    assert disposition["disposition"] == "human_only_escalation"
    assert disposition["execution_permitted"] is False


def test_sec01_clean_security_group_conflicts_with_firing_alert() -> None:
    diagnosis = diagnose_moodle_alert("MoodleDatabasePortExposure", _sec01_evidence(0.0, 0.0))
    assert diagnosis["status"] == "conflicting_evidence"


def test_sec01_without_audit_metric_fails_closed() -> None:
    diagnosis = diagnose_moodle_alert(
        "MoodleDatabasePortExposure", [_metric("moodle_rds_tcp_probe", 1.0, "monitor-ai-01", "rds")]
    )
    assert diagnosis["status"] == "insufficient_evidence"
    assert diagnosis["missing_queries"] == ["moodle_rds_sg_unreviewed_ingress_rules"]


def _observed_report(**overrides) -> dict:
    report = {
        "status": "investigation_hypothesis",
        "incident_id": "inc-1",
        "evidence_status": "complete",
        "evidence_refs": ["ev-a", "ev-b", "ev-c"],
        "evidence_sources": ["alertmanager", "prometheus"],
        "collector_errors": [],
        "diagnosis": {"status": "hypothesis", "cause_code": "x", "hypothesis": "h", "confidence": 0.8},
        "confirmed_affected_resources": ["postgres-db"],
        "potentially_affected_resources": ["moodle-app"],
        "remediation_disposition": {"disposition": "escalated_awaiting_operator_isolation"},
        "plan_candidate": None,
        "remediation_candidate": None,
        "decision_evidence_refs": ["ev-decision"],
        "execution_permitted": False,
        "resolution_eligible": False,
    }
    report.update(overrides)
    return report


def test_admin_report_for_candidate_lists_approval_and_verifier_steps() -> None:
    report = _observed_report(
        status="remediation_candidate",
        remediation_disposition={"disposition": "candidate_proposed"},
        remediation_candidate_evidence_id="ev-candidate",
        remediation_candidate={
            "scenario_id": "CON-01",
            "catalog_action_id": "start_reviewed_compose_service",
            "target_scope": "moodle-app-b",
            "action": {"rollback_plan": {"rollback_action_id": "stop_reviewed_compose_service"}},
        },
    )
    result = build_admin_action_report("MoodleWebContainerMissing", report)
    admin = "\n".join(result["admin_steps"])
    assert "/api/moodle/remediation/ev-candidate/prepare" in admin
    assert "/api/moodle/remediation/ev-candidate/execute" in admin
    assert "2 phê duyệt" in admin
    assert "Independent Verifier" in admin
    assert "stop_reviewed_compose_service" in admin
    assert any("CON-01" in step for step in result["ai_completed_steps"])
    assert result["execution_permitted"] is False
    assert result["resolution_eligible"] is False


def test_admin_report_for_sec01_gives_manual_security_steps() -> None:
    report = _observed_report(
        diagnosis={"status": "hypothesis", "cause_code": "rds_ingress_unreviewed_source", "hypothesis": "h", "confidence": 0.9},
        remediation_disposition={"disposition": "human_only_escalation"},
    )
    result = build_admin_action_report("MoodleDatabasePortExposure", report)
    assert result["headline"] == "Chỉ con người được xử lý (HUMAN_ONLY)"
    assert "Kỹ sư an ninh" in result["admin_steps"][0]
    assert not any("/execute" in step for step in result["admin_steps"])


def test_admin_report_for_missing_telemetry_points_to_exporters() -> None:
    report = _observed_report(
        diagnosis={
            "status": "insufficient_evidence",
            "reason": "required fresh Prometheus observations are missing",
            "missing_queries": ["moodle_adhoc_tasks_overdue_count"],
        },
        remediation_disposition={"disposition": "awaiting_valid_evidence"},
    )
    result = build_admin_action_report("MoodleAdhocTaskQueueStuck", report)
    assert any("moodle_adhoc_tasks_overdue_count" in step for step in result["admin_steps"])
    assert "Administrator cần thực hiện:" in result["text"]


def test_admin_report_for_escalated_alert_has_no_ai_steps() -> None:
    result = build_admin_action_report(
        "MoodleWebContainerMissing",
        {"status": "escalated", "reason": "restricted to staging", "execution_permitted": False},
    )
    assert result["ai_completed_steps"] == []
    assert "restricted to staging" in result["admin_steps"][0]


def test_failed_rds_audit_is_reported_as_blind_sec01_telemetry() -> None:
    diagnosis = diagnose_moodle_alert(
        "MoodleRdsIngressAuditFailed", [_metric("moodle_rds_sg_audit_success", 0.0, "monitor-ai-01", "audit")]
    )
    assert diagnosis["status"] == "hypothesis"
    assert diagnosis["cause_code"] == "rds_ingress_audit_unavailable"
    report = build_admin_action_report(
        "MoodleRdsIngressAuditFailed",
        _observed_report(diagnosis=diagnosis, remediation_disposition={"disposition": "escalated_awaiting_operator_isolation"}),
    )
    assert "AiopsRdsSecurityGroupId" in report["admin_steps"][0]


def test_failed_adhoc_probe_names_the_blind_node() -> None:
    diagnosis = diagnose_moodle_alert(
        "MoodleAdhocQueueProbeFailed",
        [
            _metric("moodle_adhoc_queue_probe_success", 1.0, "moodle-app-a", "probe-a"),
            _metric("moodle_adhoc_queue_probe_success", 0.0, "moodle-app-b", "probe-b"),
        ],
    )
    assert diagnosis["status"] == "hypothesis"
    assert diagnosis["cause_code"] == "adhoc_queue_probe_failed"
    assert diagnosis["affected_nodes"] == ["moodle-app-b"]
