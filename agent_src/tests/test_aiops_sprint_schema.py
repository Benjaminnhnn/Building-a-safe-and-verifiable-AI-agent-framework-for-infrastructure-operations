from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment, ResourceType
from core.schema.diagnosis import DiagnosisResult, RootCauseHypothesis
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource
from core.schema.safety import SafetyDecision
from core.schema.scenario import ScenarioGroundTruth
from core.schema.verification import ProbeResult, VerificationResult


def test_resource_inventory_matches_resource_schema() -> None:
    path = Path("evaluation/resources/moodle_resource_inventory.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    resources = [Resource(**item) for item in data["resources"]]

    ids = [item.resource_id for item in resources]
    assert len(ids) == len(set(ids))
    assert "moodle-app" in ids
    assert "postgres-db" in ids
    assert all(item.environment == Environment.STAGING for item in resources)


def test_runtime_resource_inventory_matches_evaluation_contract() -> None:
    evaluation = json.loads(
        Path("evaluation/resources/moodle_resource_inventory.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = json.loads(
        Path("agent_src/config/moodle_resource_inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert runtime == evaluation


def test_moodle_ground_truth_v2_matches_scenario_schema() -> None:
    paths = sorted(Path("evaluation/ground_truth").glob("*.json"))
    moodle_paths = [path for path in paths if path.name[:2] in {"DB", "RE", "NE", "CO", "SE"}]
    assert len(moodle_paths) >= 5

    for path in moodle_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        scenario = ScenarioGroundTruth(**data)
        assert scenario.initial_state
        assert scenario.fault_trigger
        assert scenario.observed_signals
        assert scenario.expected_root_cause["component"]
        assert scenario.allowed_actions
        assert scenario.forbidden_actions
        assert scenario.communication_contract["allowed"]
        assert scenario.rollback_plan["available"] is True


def test_core_schema_accepts_valid_minimal_pipeline_objects() -> None:
    evidence = Evidence(
        evidence_id="ev-db-01-prometheus-up",
        incident_id="inc-db-01-001",
        resource_id="postgres-db",
        source="prometheus",
        summary="PostgreSQL exporter reports the database is down.",
        raw_ref="prometheus:postgres-exporter-up",
        content_hash="sha256:test-evidence",
    )

    incident = Incident(
        incident_id="inc-db-01-001",
        fingerprint="fingerprint-db-01",
        title="Moodle cannot connect to PostgreSQL",
        severity="critical",
        affected_resources=["postgres-db", "moodle-app"],
        evidence_refs=[evidence.evidence_id],
    )

    hypothesis = RootCauseHypothesis(
        hypothesis_id="hyp-db-stopped",
        root_cause="PostgreSQL container stopped",
        confidence=0.92,
        affected_resources=["postgres-db", "moodle-app"],
        supporting_evidence_refs=[evidence.evidence_id],
        reasoning_summary="Database evidence and Moodle failure point to a stopped PostgreSQL container.",
    )

    diagnosis = DiagnosisResult(
        diagnosis_id="diag-db-01-001",
        incident_id=incident.incident_id,
        top_hypothesis=hypothesis,
        evidence_refs=[evidence.evidence_id],
        confidence=0.92,
        ready_for_planning=True,
    )

    action = TypedAction(
        action_id="act-db-01-start-container",
        incident_id=incident.incident_id,
        action_type=ActionType.START_CONTAINER,
        target_resource_id="postgres-db",
        environment=Environment.STAGING,
        reason=diagnosis.top_hypothesis.reasoning_summary,
        evidence_refs=[evidence.evidence_id],
        parameters={"container_name": "postgres"},
        preconditions=["target resource exists", "environment is staging"],
        expected_outcome="PostgreSQL accepts connections and Moodle synthetic transaction passes.",
        reversible=True,
        rollback_plan=RollbackPlan(
            available=True,
            method="Stop the restarted container if related probes fail.",
            expected_duration_seconds=30,
        ),
    )

    safety = SafetyDecision(
        decision_id="gate-act-db-01",
        action_id=action.action_id,
        incident_id=incident.incident_id,
        decision="allow",
        reasons=["staging environment", "single reversible target"],
        required_evidence_refs=[evidence.evidence_id],
    )

    verification = VerificationResult(
        verification_id="verify-db-01",
        incident_id=incident.incident_id,
        health_passed=True,
        communication_contract_passed=True,
        stability_seconds=120,
        allowed_probes=[ProbeResult(name="moodle_to_database", passed=True, details="connection ok")],
        forbidden_probes=[ProbeResult(name="internet_to_postgresql", passed=True, details="blocked")],
        related_probes=[ProbeResult(name="alertmanager_to_agent", passed=True, details="delivery ok")],
        verdict="resolved",
    )

    assert safety.decision == "allow"
    assert verification.verdict == "resolved"


def test_action_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TypedAction(
            action_id="act-invalid",
            incident_id="inc-invalid",
            action_type=ActionType.START_CONTAINER,
            target_resource_id="postgres-db",
            environment=Environment.STAGING,
            reason="Missing evidence should fail.",
            evidence_refs=[],
            expected_outcome="Should not be accepted.",
            reversible=True,
            rollback_plan=RollbackPlan(available=True),
        )


def test_resource_type_enum_accepts_expected_values() -> None:
    resource = Resource(
        resource_id="moodle-app",
        name="Moodle application",
        type=ResourceType.APPLICATION,
        environment=Environment.STAGING,
        owner_role="infrastructure_engineer",
    )
    assert resource.type == ResourceType.APPLICATION
