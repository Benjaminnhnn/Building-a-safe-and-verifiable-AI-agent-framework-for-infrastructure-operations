from __future__ import annotations

from core.action_catalog import ActionCatalog
from core.moodle_planner import plan_moodle_investigation


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
