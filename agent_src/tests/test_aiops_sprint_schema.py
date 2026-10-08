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


def test_scenario_ground_truth_rejects_duplicate_or_blank_observed_signals() -> None:
    source = json.loads(Path("evaluation/ground_truth/moodle/DB-02.json").read_text(encoding="utf-8"))
    source["observed_signals"] = ["same", "same"]
    with pytest.raises(ValidationError, match="observed_signals must be unique"):
        ScenarioGroundTruth(**source)
    source["observed_signals"] = [" "]
    with pytest.raises(ValidationError, match="observed_signals must not contain blank values"):
        ScenarioGroundTruth(**source)


def test_verifier_contract_booleans_reject_integer_coercion() -> None:
    from datetime import datetime, timezone
    from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult

    with pytest.raises(ValidationError):
        ProbeResult(name="probe", passed=1, details="invalid coercion")
    with pytest.raises(ValidationError):
        StabilityObservation(observed_at=datetime.now(timezone.utc), healthy=1)
    with pytest.raises(ValidationError):
        VerificationResult(
            verification_id="verify-1",
            incident_id="incident-1",
            health_passed=1,
            communication_contract_passed=True,
            stability_seconds=0,
            verdict="not_resolved",
        )
    with pytest.raises(ValidationError):
        VerificationResult(
            verification_id="verify-1",
            incident_id="incident-1",
            health_passed=True,
            communication_contract_passed=True,
            stability_seconds=True,
            verdict="not_resolved",
        )

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
        action_hash=action.action_hash,
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
            rollback_plan=RollbackPlan(available=True, method="Restore the prior service state."),
        )


def test_typed_action_and_rollback_plan_reject_unknown_fields() -> None:
    action = TypedAction(
        action_id="act-strict",
        incident_id="inc-strict",
        action_type=ActionType.START_CONTAINER,
        target_resource_id="postgres-db",
        environment=Environment.STAGING,
        reason="Evidence-backed test action.",
        evidence_refs=["ev-strict"],
        expected_outcome="Service probe succeeds.",
        reversible=True,
        rollback_plan=RollbackPlan(available=True, method="Stop the started container."),
    )
    action_payload = action.model_dump()
    action_payload["unreviewed_executor"] = "shell"
    with pytest.raises(ValidationError):
        TypedAction.model_validate(action_payload)

    rollback_payload = action.model_dump()
    rollback_payload["rollback_plan"]["unreviewed_command"] = "rm -rf /data"
    with pytest.raises(ValidationError):
        TypedAction.model_validate(rollback_payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available", "false"),
        ("available", 1),
        ("expected_duration_seconds", True),
        ("expected_duration_seconds", 0),
        ("expected_duration_seconds", -1),
    ],
)
def test_rollback_plan_rejects_coerced_or_impossible_values(field: str, value: object) -> None:
    payload = {"available": True, "method": "Restore the pre-action service state."}
    payload[field] = value
    with pytest.raises(ValidationError):
        RollbackPlan(**payload)


def test_available_rollback_plan_requires_a_nonblank_method() -> None:
    for method in (None, "", "   "):
        with pytest.raises(ValidationError, match="nonblank method"):
            RollbackPlan(available=True, method=method)


@pytest.mark.parametrize("value", ["false", 0, 1])
def test_action_safety_flags_require_boolean_values(value: object) -> None:
    with pytest.raises(ValidationError):
        TypedAction(
            action_id="act-strict-flag",
            incident_id="inc-strict-flag",
            action_type=ActionType.READ_HEALTH,
            target_resource_id="moodle-app",
            environment=Environment.STAGING,
            reason="Read-only probe proposal.",
            evidence_refs=["ev-strict-flag"],
            expected_outcome="Collect health evidence.",
            reversible=value,
            rollback_plan=RollbackPlan(available=False),
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
