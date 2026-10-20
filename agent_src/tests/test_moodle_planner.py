from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from core.evidence.adapters import SafeLiveActionRequest
from core.safety.action_catalog import ActionCatalog
from core.moodle.planner import plan_moodle_investigation, plan_moodle_s3_3_remediation
from core.safety.safe_execution_gate import SafeExecutionGate
from core.schema.action import TypedAction


def test_planner_creates_read_only_evidence_bound_proposal() -> None:
    action = plan_moodle_investigation(
        incident_id="inc-1",
        diagnosis={
            "status": "hypothesis",
            "cause_code": "rds_path_probe_failed",
            "evidence_refs": ["ev-synthetic", "ev-rds"],
        },
    )

    assert action is not None
    assert action.action_type == "read_health"
    assert action.target_resource_id == "postgres-db"
    assert action.evidence_refs == ["ev-rds", "ev-synthetic"]
    assert action.parameters == {
        "catalog_action_id": "verify_tls_database_connection",
        "target_scope": "staging_moodle_nodes",
    }
    entry = ActionCatalog.load_moodle_catalog().lookup(action.parameters["catalog_action_id"])
    assert entry is not None and entry.adapter == "probe" and entry.permission.read_only
    assert action.requires_approval is True
    assert action.rollback_plan.available is False


def test_planner_refuses_non_hypotheses_unknown_rules_and_missing_evidence() -> None:
    cases = [
        {"status": "insufficient_evidence", "cause_code": "rds_path_probe_failed", "evidence_refs": ["ev-1"]},
        {"status": "hypothesis", "cause_code": "unreviewed_rule", "evidence_refs": ["ev-1"]},
        {"status": "hypothesis", "cause_code": "rds_path_probe_failed", "evidence_refs": []},
    ]

    for diagnosis in cases:
        assert plan_moodle_investigation(incident_id="inc-1", diagnosis=diagnosis) is None


def test_node_rds_hypothesis_only_proposes_read_health():
    action = plan_moodle_investigation(
        incident_id="inc-node-rds",
        diagnosis={
            "status": "hypothesis",
            "cause_code": "node_rds_path_probe_failed",
            "evidence_refs": ["ev-node-a", "ev-node-b"],
            "affected_nodes": ["moodle-app-b"],
        },
    )

    assert action is not None
    assert action.action_type == "read_health"
    assert action.target_resource_id == "postgres-db"
    assert action.evidence_refs == ["ev-node-a", "ev-node-b"]
    assert action.requires_approval is True
    assert action.rollback_plan.available is False
    assert action.parameters["catalog_action_id"] == "verify_tls_database_connection"


def test_public_entry_hypothesis_uses_catalogued_http_probe():
    action = plan_moodle_investigation(
        incident_id="inc-public",
        diagnosis={
            "status": "hypothesis",
            "cause_code": "public_entry_probe_failed",
            "evidence_refs": ["ev-public"],
        },
    )

    assert action is not None
    assert action.target_resource_id == "moodle-web-endpoint"
    assert action.parameters["catalog_action_id"] == "probe_moodle_http_health"
    assert action.parameters["target_scope"] == "moodle-web-endpoint"


def test_efs_mount_hypothesis_has_no_unapproved_read_only_probe():
    assert plan_moodle_investigation(
        incident_id="inc-efs",
        diagnosis={
            "status": "hypothesis",
            "cause_code": "efs_mount_probe_failed",
            "evidence_refs": ["ev-efs"],
        },
    ) is None


def _metric(query_id: str, value: float, instance: str, suffix: str):
    return SimpleNamespace(
        source="prometheus",
        evidence_id=f"ev-{suffix}",
        collected_at=datetime.now(timezone.utc),
        metadata={"payload_json": json.dumps({
            "query_id": query_id,
            "sample_value": value,
            "labels": {"instance": instance},
        })},
    )


def _s3_3_evidence(scenario: str):
    metrics = {
        "DB-01": [
            ("moodle_node_rds_tcp_success", 0, "moodle-app-a"),
            ("moodle_node_rds_tcp_success", 0, "moodle-app-b"),
            ("moodle_fault_db01_tagged_rule", 1, "moodle-app-a"),
            ("moodle_fault_db01_tagged_rule", 1, "moodle-app-b"),
        ],
        "RES-01": [
            ("moodle_node_cpu_busy_percent", 22, "moodle-app-a"),
            ("moodle_node_cpu_busy_percent", 82, "moodle-app-b"),
            ("moodle_fault_res01_cpu_fixture_running", 0, "moodle-app-a"),
            ("moodle_fault_res01_cpu_fixture_running", 1, "moodle-app-b"),
            ("moodle_node_web_running", 1, "moodle-app-a"),
            ("moodle_node_web_running", 1, "moodle-app-b"),
        ],
        "CON-01": [
            ("moodle_fault_con01_web_stopped", 0, "moodle-app-a"),
            ("moodle_fault_con01_web_stopped", 1, "moodle-app-b"),
            ("moodle_node_web_running", 1, "moodle-app-a"),
            ("moodle_node_web_running", 0, "moodle-app-b"),
            ("moodle_node_up", 1, "moodle-app-a"),
            ("moodle_node_up", 1, "moodle-app-b"),
            ("moodle_cadvisor_up", 1, "moodle-app-a"),
            ("moodle_cadvisor_up", 1, "moodle-app-b"),
        ],
        "NET-01": [
            ("moodle_synthetic_success", 0, "monitor-ai-01"),
            ("moodle_rds_tcp_probe", 1, "monitor-ai-01"),
            ("moodle_public_probe", 1, "monitor-ai-01"),
            ("moodle_efs_mount", 1, "moodle-app-a"),
            ("moodle_efs_mount", 1, "moodle-app-b"),
            ("moodle_fault_net01_hosts_override", 1, "moodle-app-a"),
            ("moodle_fault_net01_hosts_override", 1, "moodle-app-b"),
        ],
        "SEC-02": [
            ("moodle_synthetic_success", 0, "monitor-ai-01"),
            ("moodle_rds_tcp_probe", 1, "monitor-ai-01"),
            ("moodle_public_probe", 1, "monitor-ai-01"),
            ("moodle_efs_mount", 1, "moodle-app-a"),
            ("moodle_efs_mount", 1, "moodle-app-b"),
            ("moodle_fault_sec02_fixture_mode_zero", 1, "moodle-app-a"),
            ("moodle_fault_sec02_fixture_mode_zero", 0, "moodle-app-b"),
        ],
    }
    return [
        _metric(query_id, value, instance, f"{scenario}-{index}")
        for index, (query_id, value, instance) in enumerate(metrics[scenario])
    ]


def test_s3_3_plans_require_exact_fresh_runtime_fault_evidence():
    alerts = {
        "DB-01": "MoodleNodeRdsTcpFailed",
        "RES-01": "MoodleNodeCpuHigh",
        "NET-01": "MoodleSyntheticTransactionFailed",
        "CON-01": "MoodleWebContainerMissing",
        "SEC-02": "MoodleSyntheticTransactionFailed",
    }
    causes = {
        "DB-01": "node_rds_path_probe_failed",
        "RES-01": "node_cpu_pressure",
        "NET-01": "application_path_not_isolated",
        "CON-01": "web_container_not_running",
        "SEC-02": "application_path_not_isolated",
    }
    for scenario, alert_name in alerts.items():
        evidence = _s3_3_evidence(scenario)
        proposal = plan_moodle_s3_3_remediation(
            incident_id=f"inc-{scenario}",
            alert_name=alert_name,
            diagnosis={
                "status": "hypothesis",
                "cause_code": causes[scenario],
                "evidence_refs": [item.evidence_id for item in evidence],
            },
            evidence=evidence,
        )
        assert proposal is not None
        assert proposal["scenario_id"] == scenario
        assert proposal["approval_required"] is True
        assert proposal["execution_permitted"] is False
        assert proposal["resolution_eligible"] is False
        assert len(proposal["action"]["evidence_refs"]) >= 3
        request = SafeLiveActionRequest(
            request_id=f"req-{scenario}",
            idempotency_key=proposal["action"]["idempotency_key"],
            catalog_action_id=proposal["catalog_action_id"],
            scenario_id=proposal["scenario_id"],
            target_scope=proposal["target_scope"],
            action=TypedAction.model_validate(proposal["action"]),
        )
        gate = SafeExecutionGate(
            approval_signing_key="test-key",
            approved_actors={"operator"},
            catalog=ActionCatalog.load_moodle_s3_3_execution_catalog(),
            rollback_catalog=ActionCatalog.load_full_catalog(),
        )
        verdict = gate.evaluate(request, actor_role="executor", confidence=0.9)
        assert verdict.decision == "REQUIRE_APPROVAL"


def test_s3_3_planner_fails_closed_for_missing_or_stale_marker():
    from datetime import timedelta

    evidence = [
        item for item in _s3_3_evidence("RES-01")
        if json.loads(item.metadata["payload_json"])["query_id"] != "moodle_fault_res01_cpu_fixture_running"
    ]
    assert plan_moodle_s3_3_remediation(
        incident_id="inc-no-marker",
        alert_name="MoodleNodeCpuHigh",
        diagnosis={"status": "hypothesis", "cause_code": "node_cpu_pressure", "evidence_refs": [item.evidence_id for item in evidence]},
        evidence=evidence,
    ) is None

    stale = _s3_3_evidence("RES-01")
    marker = next(
        item for item in stale
        if json.loads(item.metadata["payload_json"])["query_id"] == "moodle_fault_res01_cpu_fixture_running"
        and json.loads(item.metadata["payload_json"])["labels"]["instance"] == "moodle-app-b"
    )
    marker.collected_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    assert plan_moodle_s3_3_remediation(
        incident_id="inc-stale-marker",
        alert_name="MoodleNodeCpuHigh",
        diagnosis={"status": "hypothesis", "cause_code": "node_cpu_pressure", "evidence_refs": [item.evidence_id for item in stale]},
        evidence=stale,
    ) is None
