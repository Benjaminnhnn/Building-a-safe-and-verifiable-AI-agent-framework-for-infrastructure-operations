"""Comprehensive tests for modular roles and the sequential pipeline orchestrator.

Test Coverage:
1. test_orchestrator_halts_when_gate_rejects (DENY and unapproved REQUIRE_APPROVAL).
2. test_incident_id_and_evidence_ids_propagated (end-to-end trace across all stages).
3. test_diagnosis_insufficient_evidence_halts_pipeline.
4. test_executor_requires_gate_pass.
5. test_verifier_read_only_isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from evaluation.agent_replay.domain.action_plan import ActionPlan, BlastRadius, ExpectedScope, ReversibleRollback
from evaluation.agent_replay.domain.approval import ApprovalRecord, ApprovalScope, ApprovalStore
from evaluation.agent_replay.domain.dry_run import DictReadOnlyClient, ReadOnlyTargetClient
from evaluation.agent_replay.domain.enums import ActionType, Environment, EvidenceSource
from evaluation.agent_replay.domain.evidence import Evidence
from evaluation.agent_replay.domain.gate_pass import GatePass
from evaluation.agent_replay.domain.incident import Incident, IncidentStatus
from evaluation.agent_replay.domain.resource import Resource
from evaluation.agent_replay.domain.safety_gate import PolicyDecision, SafetyGate
from evaluation.agent_replay.roles.contracts import BaseRoleMessage
from evaluation.agent_replay.roles.diagnosis import DiagnosisInput, DiagnosisOutput, DiagnosisRole
from evaluation.agent_replay.roles.executor import ExecutorInput, ExecutorOutput, ExecutorRole
from evaluation.agent_replay.roles.observer import ObserverInput, ObserverOutput, ObserverRole
from evaluation.agent_replay.roles.orchestrator import (
    PipelineExecutionResult,
    PipelineRunInput,
    SequentialPipelineOrchestrator,
)
from evaluation.agent_replay.roles.planner import PlannerInput, PlannerOutput, PlannerRole
from evaluation.agent_replay.roles.safety_gate import SafetyGateInput, SafetyGateOutput, SafetyGateRole
from evaluation.agent_replay.roles.verifier import VerifierInput, VerifierOutput, VerifierRole
from evaluation.agent_replay.verification.communication_contract import (
    CommunicationContract,
    ConnectionRule,
    ServiceDefinition,
)
from evaluation.agent_replay.verification.independent_verifier import (
    HealthCheckResult,
    ProbeStatus,
    ScenarioProbeResult,
    VerificationReport,
    VerifierVerdict,
)


# =============================================================================
# FIXTURES & HELPERS
# =============================================================================

@pytest.fixture
def sample_alertmanager_payload() -> dict[str, Any]:
    return {
        "version": "4",
        "groupKey": "{}:{alertname=\"MoodleEndpointDown\"}",
        "status": "firing",
        "receiver": "aiops-webhook",
        "groupLabels": {"alertname": "MoodleEndpointDown"},
        "commonLabels": {
            "alertname": "MoodleEndpointDown",
            "service": "moodle-app",
            "severity": "critical",
        },
        "commonAnnotations": {
            "summary": "Moodle web endpoint is failing health check",
            "description": "HTTP 502 returned from web container",
        },
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "MoodleEndpointDown",
                    "signal": "moodle_endpoint_down",
                    "service": "moodle-app",
                    "resource_id": "moodle-app",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "moodle_endpoint_down detected on moodle-app",
                },
                "startsAt": "2026-10-15T12:00:00Z",
                "fingerprint": "a1b2c3d4e5f60718",
            }
        ],
    }


@pytest.fixture
def sample_contract() -> CommunicationContract:
    return CommunicationContract(
        version="1.0",
        contract_name="test_contract",
        target_system="demo_web",
        sample_only=True,
        services={
            "moodle-app": ServiceDefinition(
                health_endpoint="http://localhost:8080/healthz",
                required_flows=["postgres-db"],
            ),
            "postgres-db": ServiceDefinition(
                health_endpoint="tcp://localhost:5432",
                required_flows=[],
            ),
        },
        allowed_connections=[
            ConnectionRule(
                id="rule-app-db",
                source="moodle-app",
                destination="postgres-db",
                port=5432,
                protocol="tcp",
            ),
        ],
        forbidden_connections=[
            ConnectionRule(
                id="rule-external-db",
                source="internet",
                destination="postgres-db",
                port=5432,
                protocol="tcp",
            ),
        ],
    )


class MockExecutableTargetClient:
    """Mock target client for dry-run and execution."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        self.state = initial_state or {"container_status": "exited", "active_connections": 0}
        self.executed_actions: list[dict[str, Any]] = []

    def get_resource_state(self, resource_id: str) -> dict[str, Any]:
        return dict(self.state)

    def execute_action(self, plan: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.executed_actions.append({
            "plan": plan,
            "args": args,
            "kwargs": kwargs,
        })
        self.state["container_status"] = "running"
        return {"status": "ok", "action": getattr(plan, "action_type", "unknown")}


def create_mock_probes_factory(healthy: bool = True):
    def factory(service, target, scenario_id):
        class MockProbeSuite:
            def check_health(self, s):
                return HealthCheckResult(
                    service=s.name,
                    healthy=healthy,
                    status_code=200 if healthy else 500,
                    latency_ms=10.0,
                    checked_at=datetime.now(timezone.utc),
                )

            def check_connection(self, rule):
                from evaluation.agent_replay.verification.independent_verifier import ConnectionProbeResult
                return ConnectionProbeResult(
                    source=rule.source,
                    destination=rule.destination,
                    port=rule.port,
                    protocol=rule.protocol,
                    reachable=healthy,
                    latency_ms=5.0,
                    checked_at=datetime.now(timezone.utc),
                )

            def check_scenario_probe(self, scen_id):
                return ScenarioProbeResult(
                    scenario_id=scen_id,
                    status=ProbeStatus.SUCCESS if healthy else ProbeStatus.FAILED,
                    output="Synthetic check ok" if healthy else "Error",
                    latency_ms=25.0,
                    checked_at=datetime.now(timezone.utc),
                )

        return MockProbeSuite()

    return factory



    planner = PlannerRole()
    # Verify planner has no execute method
    assert not hasattr(planner, "execute"), "Planner must NOT have an execute method"
    assert not hasattr(planner, "run_action"), "Planner must NOT have run_action"
    assert hasattr(planner, "plan"), "Planner must expose plan method"


# =============================================================================
# TEST 2: ORCHESTRATOR HALTS WHEN GATE REJECTS (DENY & REQUIRE_APPROVAL)
# =============================================================================

def test_orchestrator_halts_when_gate_denies(
    sample_alertmanager_payload: dict[str, Any],
    sample_contract: CommunicationContract,
) -> None:
    """When Safety Gate returns DENY, execution halts immediately. Executor & Verifier are never called."""
    mock_target = MockExecutableTargetClient()
    dry_run_client = DictReadOnlyClient(resources={"moodle-app": {"status": "exited"}})

    # Create a mock Safety Gate that always denies
    mock_gate = MagicMock(spec=SafetyGateRole)
    mock_gate.evaluate.return_value = SafetyGateOutput(
        incident_id="inc-mock",
        evidence_ids=["evi-1"],
        gate_result=MagicMock(),
        decision=PolicyDecision.DENY,
        gate_pass=None,
        reason_code="BLOCKED_BY_SECURITY_POLICY",
    )

    mock_executor = MagicMock(spec=ExecutorRole)
    mock_verifier = MagicMock(spec=VerifierRole)

    orchestrator = SequentialPipelineOrchestrator(
        safety_gate=mock_gate,
        executor=mock_executor,
        verifier=mock_verifier,
    )

    result = orchestrator.run(
        PipelineRunInput(
            alert_payload=sample_alertmanager_payload,
            scenario_id="scenario_01",
            dry_run_client=dry_run_client,
            target_client=mock_target,
            contract=sample_contract,
        )
    )

    # Invariants
    assert result.halted is True
    assert result.halted_at_stage == "safety_gate"
    assert "DENY" in (result.halt_reason or "")
    assert "safety_gate" in result.stages_completed
    assert "executor" not in result.stages_completed
    assert "verifier" not in result.stages_completed

    # Neither executor nor verifier must have been called
    mock_executor.execute.assert_not_called()
    mock_verifier.verify.assert_not_called()
    assert len(mock_target.executed_actions) == 0


def test_orchestrator_halts_when_gate_requires_approval_without_pass(
    sample_alertmanager_payload: dict[str, Any],
    sample_contract: CommunicationContract,
) -> None:
    """When Safety Gate returns REQUIRE_APPROVAL without an approved GatePass, pipeline halts."""
    mock_target = MockExecutableTargetClient()
    dry_run_client = DictReadOnlyClient(resources={"moodle-app": {"status": "exited"}})

    mock_gate = MagicMock(spec=SafetyGateRole)
    mock_gate.evaluate.return_value = SafetyGateOutput(
        incident_id="inc-mock",
        evidence_ids=["evi-1"],
        gate_result=MagicMock(),
        decision=PolicyDecision.REQUIRE_APPROVAL,
        gate_pass=None,
        reason_code="APPROVAL_REQUIRED_FOR_PRODUCTION",
    )

    mock_executor = MagicMock(spec=ExecutorRole)
    mock_verifier = MagicMock(spec=VerifierRole)

    orchestrator = SequentialPipelineOrchestrator(
        safety_gate=mock_gate,
        executor=mock_executor,
        verifier=mock_verifier,
    )

    result = orchestrator.run(
        PipelineRunInput(
            alert_payload=sample_alertmanager_payload,
            scenario_id="scenario_01",
            dry_run_client=dry_run_client,
            target_client=mock_target,
            contract=sample_contract,
        )
    )

    assert result.halted is True
    assert result.halted_at_stage == "safety_gate"
    assert "REQUIRE_APPROVAL" in (result.halt_reason or "")
    assert result.incident.status == IncidentStatus.GATED
    mock_executor.execute.assert_not_called()
    mock_verifier.verify.assert_not_called()


# =============================================================================
# TEST 3: INCIDENT_ID AND EVIDENCE_IDS PROPAGATED END-TO-END
# =============================================================================

def test_incident_id_and_evidence_ids_propagated_throughout_pipeline(
    sample_alertmanager_payload: dict[str, Any],
    sample_contract: CommunicationContract,
) -> None:
    """incident_id and evidence_ids must be present and identical in all stage message outputs."""
    mock_target = MockExecutableTargetClient()
    dry_run_client = DictReadOnlyClient(resources={"moodle-app": {"status": "exited"}})
    probes_factory = create_mock_probes_factory(healthy=True)

    orchestrator = SequentialPipelineOrchestrator()

    result = orchestrator.run(
        PipelineRunInput(
            alert_payload=sample_alertmanager_payload,
            scenario_id="scenario_01",
            dry_run_client=dry_run_client,
            target_client=mock_target,
            contract=sample_contract,
            health_probe_fn=lambda svc: (True, "healthy"),
            allowed_probe_fn=lambda rule: (True, "reachable"),
            forbidden_probe_fn=lambda rule: (True, "blocked as expected"),
            scenario_probe_fn=lambda scen: (True, "synthetic check ok"),
            n_consecutive_checks=2,
            incident_id="inc-test-correlation-001",
        )
    )

    assert result.halted is False
    assert result.stages_completed == [
        "observer",
        "diagnosis",
        "planner",
        "safety_gate",
        "executor",
        "verifier",
    ]

    # Check incident_id uniformity across every stage
    target_id = "inc-test-correlation-001"
    assert result.incident_id == target_id
    assert result.incident.incident_id == target_id
    assert result.observer_output is not None
    assert result.observer_output.incident_id == target_id
    assert result.diagnosis_output is not None
    assert result.diagnosis_output.incident_id == target_id
    assert result.planner_output is not None
    assert result.planner_output.incident_id == target_id
    assert result.safety_gate_output is not None
    assert result.safety_gate_output.incident_id == target_id
    assert result.executor_output is not None
    assert result.executor_output.incident_id == target_id
    assert result.verifier_output is not None
    assert result.verifier_output.incident_id == target_id

    # Check evidence_ids uniformity and propagation
    assert len(result.evidence_ids) > 0
    assert result.observer_output.evidence_ids == result.evidence_ids
    assert result.diagnosis_output.evidence_ids == result.evidence_ids
    assert result.planner_output.evidence_ids == result.evidence_ids
    assert result.safety_gate_output.evidence_ids == result.evidence_ids
    assert result.executor_output.evidence_ids == result.evidence_ids
    assert result.verifier_output.evidence_ids == result.evidence_ids

    # Final verdict must be RESOLVED
    assert result.incident.status == IncidentStatus.RESOLVED
    assert result.verifier_output.verdict == VerifierVerdict.RESTORED


# =============================================================================
# TEST 4: DIAGNOSIS INSUFFICIENT EVIDENCE HALTS PIPELINE
# =============================================================================

def test_diagnosis_insufficient_evidence_halts_pipeline(
    sample_contract: CommunicationContract,
) -> None:
    """When an alert has zero recognized signals, diagnosis fails and pipeline halts."""
    unknown_alert_payload = {
        "alerts": [
            {
                "labels": {"alertname": "TotallyRandomUnknownAlert"},
                "annotations": {"summary": "Something weird happened"},
                "startsAt": "2026-10-15T12:00:00Z",
                "fingerprint": "9999999999999999",
            }
        ]
    }

    mock_target = MockExecutableTargetClient()
    dry_run_client = DictReadOnlyClient(resources={"moodle-app": {"status": "ok"}})

    orchestrator = SequentialPipelineOrchestrator()
    result = orchestrator.run(
        PipelineRunInput(
            alert_payload=unknown_alert_payload,
            scenario_id="scenario_01",
            dry_run_client=dry_run_client,
            target_client=mock_target,
            contract=sample_contract,
        )
    )

    assert result.halted is True
    assert result.halted_at_stage == "diagnosis"
    assert result.stages_completed == ["observer", "diagnosis"]
    assert result.incident.status == IncidentStatus.ESCALATED
    assert result.planner_output is None
    assert result.executor_output is None
