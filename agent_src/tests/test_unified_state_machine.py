from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from core.contract_validation import (
    ContractValidationError,
    validate_pipeline_references,
)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import (
    ActionDecision,
    ActionStatus,
    ActionType,
    Environment,
    IncidentStatus,
    ResourceType,
)
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource
from core.schema.verification import (
    ProbeResult,
    StabilityObservation,
    VerificationResult,
)
from core.state_machine import (
    IncidentStateMachine,
    StateTransitionError,
    apply_gate_decision,
)


def _incident() -> Incident:
    return Incident(
        incident_id="inc-db-01",
        fingerprint="fp-db-01",
        title="PostgreSQL stopped",
        severity="critical",
        affected_resources=["postgres-db"],
    )


def _advance_to_verifying(incident: Incident) -> None:
    machine = IncidentStateMachine()
    for state in (
        IncidentStatus.TRIAGED,
        IncidentStatus.PLANNED,
        IncidentStatus.GATED,
        IncidentStatus.EXECUTED,
        IncidentStatus.VERIFYING,
    ):
        machine.transition(incident, state, actor="test", reason="setup")


def _evidence(incident_id: str = "inc-db-01") -> Evidence:
    return Evidence(
        evidence_id="ev-db-01",
        incident_id=incident_id,
        resource_id="postgres-db",
        source="fixture",
        summary="PostgreSQL is not reachable.",
        content_hash="sha256:test",
    )


def _resource(environment: Environment = Environment.STAGING) -> Resource:
    return Resource(
        resource_id="postgres-db",
        name="PostgreSQL",
        type=ResourceType.DATABASE,
        environment=environment,
        owner_role="infrastructure_engineer",
    )


def _action(**overrides: object) -> TypedAction:
    values = {
        "action_id": "act-db-01",
        "incident_id": "inc-db-01",
        "action_type": ActionType.START_CONTAINER,
        "target_resource_id": "postgres-db",
        "environment": Environment.STAGING,
        "reason": "Evidence shows PostgreSQL stopped.",
        "evidence_refs": ["ev-db-01"],
        "expected_outcome": "Database becomes reachable.",
        "reversible": True,
        "rollback_plan": RollbackPlan(available=True, method="stop container"),
    }
    values.update(overrides)
    return TypedAction(**values)


def _verification(incident_id: str = "inc-db-01", **overrides: object) -> VerificationResult:
    checked_at = datetime.now(timezone.utc)
    values = {
        "verification_id": "verify-db-01",
        "incident_id": incident_id,
        "health_passed": True,
        "communication_contract_passed": True,
        "stability_seconds": 120,
        "allowed_probes": [ProbeResult(name="moodle", passed=True, simulated=False, details="ok")],
        "forbidden_probes": [ProbeResult(name="public_db", passed=True, simulated=False, details="blocked")],
        "related_probes": [ProbeResult(name="monitoring", passed=True, simulated=False, details="ok")],
        "stability_observations": [
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=120), healthy=True, simulated=False
            ),
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=60), healthy=True, simulated=False
            ),
            StabilityObservation(observed_at=checked_at, healthy=True, simulated=False),
        ],
        "verdict": "resolved",
        "resolution_eligible": True,
        "simulated": False,
    }
    values.update(overrides)
    return VerificationResult(**values)


def test_full_valid_transition_path_requires_independent_verifier() -> None:
    incident = _incident()
    machine = IncidentStateMachine()
    path = [
        (IncidentStatus.TRIAGED, "observer"),
        (IncidentStatus.PLANNED, "planner"),
        (IncidentStatus.GATED, "safety_gate"),
        (IncidentStatus.EXECUTED, "executor"),
        (IncidentStatus.VERIFYING, "orchestrator"),
    ]

    events = [
        machine.transition(incident, state, actor=actor, reason="test", evidence_refs=["ev-db-01"])
        for state, actor in path
    ]
    events.append(
        machine.resolve_verified(
            incident,
            _verification(),
            reason="verification passed",
            evidence_refs=["ev-db-01"],
        )
    )

    assert incident.status == IncidentStatus.RESOLVED
    assert incident.resolved_by_verifier is True
    assert [event.new_state for event in events] == [
        *(state for state, _ in path),
        IncidentStatus.RESOLVED,
    ]


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (IncidentStatus.OPEN, IncidentStatus.PLANNED),
        (IncidentStatus.TRIAGED, IncidentStatus.EXECUTED),
        (IncidentStatus.PLANNED, IncidentStatus.RESOLVED),
        (IncidentStatus.GATED, IncidentStatus.RESOLVED),
        (IncidentStatus.RESOLVED, IncidentStatus.OPEN),
        (IncidentStatus.FAILED, IncidentStatus.OPEN),
    ],
)
def test_invalid_transitions_are_rejected(current: IncidentStatus, requested: IncidentStatus) -> None:
    incident = _incident()
    machine = IncidentStateMachine()
    if current == IncidentStatus.RESOLVED:
        _advance_to_verifying(incident)
        machine.resolve_verified(incident, _verification(), reason="setup")
    elif current == IncidentStatus.FAILED:
        machine.transition(incident, current, actor="test", reason="setup")
    elif current != IncidentStatus.OPEN:
        path = {
            IncidentStatus.TRIAGED: [IncidentStatus.TRIAGED],
            IncidentStatus.PLANNED: [IncidentStatus.TRIAGED, IncidentStatus.PLANNED],
            IncidentStatus.GATED: [IncidentStatus.TRIAGED, IncidentStatus.PLANNED, IncidentStatus.GATED],
        }[current]
        for state in path:
            machine.transition(incident, state, actor="test", reason="setup")

    with pytest.raises(StateTransitionError) as exc_info:
        machine.transition(incident, requested, actor="planner", reason="invalid")

    assert exc_info.value.current_state == current
    assert exc_info.value.requested_state == requested
    if requested == IncidentStatus.RESOLVED:
        assert "resolve_verified" in exc_info.value.reason
    else:
        assert exc_info.value.reason == "invalid"


@pytest.mark.parametrize("actor", ["planner", "executor", "orchestrator", "independent_verifier"])
def test_only_verifier_can_resolve(actor: str) -> None:
    incident = _incident()
    _advance_to_verifying(incident)
    with pytest.raises(StateTransitionError, match="resolve_verified"):
        IncidentStateMachine().transition(
            incident,
            IncidentStatus.RESOLVED,
            actor=actor,
            reason="not authorized",
        )


@pytest.mark.parametrize(
    "verification",
    [
        _verification(resolution_eligible=False),
        _verification(verdict="false_recovery", communication_contract_passed=False),
        _verification(health_passed=False),
        _verification(stability_seconds=119),
        _verification(stability_observations=[]),
        _verification(
            stability_observations=[
                StabilityObservation(
                    observed_at=datetime.now(timezone.utc) - timedelta(seconds=300),
                    healthy=True,
                ),
                StabilityObservation(
                    observed_at=datetime.now(timezone.utc) - timedelta(seconds=240),
                    healthy=True,
                ),
                StabilityObservation(
                    observed_at=datetime.now(timezone.utc) - timedelta(seconds=180),
                    healthy=True,
                ),
            ]
        ),
        _verification(
            stability_observations=[
                StabilityObservation(
                    observed_at=datetime.now(timezone.utc) - timedelta(seconds=120),
                    healthy=True,
                ),
                StabilityObservation(observed_at=datetime.now(timezone.utc), healthy=True),
            ]
        ),
        _verification(allowed_probes=[ProbeResult(name="moodle", passed=False, details="down")]),
        _verification(forbidden_probes=[]),
        _verification(related_probes=[]),
        _verification(related_probes=[
            ProbeResult(name="monitoring", passed=True, simulated=False, details="ok"),
            ProbeResult(name="monitoring", passed=True, simulated=False, details="duplicate"),
        ]),
        _verification(allowed_probes=[ProbeResult(name="moodle", passed=True, simulated=True, details="fixture")]),
        _verification(stability_observations=[StabilityObservation(
            observed_at=datetime.now(timezone.utc) - timedelta(seconds=120),
            healthy=True,
            simulated=True,
        ), StabilityObservation(
            observed_at=datetime.now(timezone.utc), healthy=True, simulated=False
        )]),
        _verification(incident_id="another-incident"),
    ],
)
def test_resolution_requires_eligible_matching_verifier_evidence(
    verification: VerificationResult,
) -> None:
    incident = _incident()
    _advance_to_verifying(incident)
    with pytest.raises(StateTransitionError):
        IncidentStateMachine().resolve_verified(
            incident, verification, reason="must fail closed"
        )


def test_simulated_verifier_result_ends_as_verified_dry_run_not_resolved() -> None:
    incident = _incident()
    _advance_to_verifying(incident)

    IncidentStateMachine().resolve_verified(
        incident,
        _verification(simulated=True),
        reason="fixture replay passed",
    )

    assert incident.status == IncidentStatus.VERIFIED_DRY_RUN
    assert incident.resolved_by_verifier is False


def test_resolution_revalidates_verifier_model_copy_before_transition() -> None:
    incident = _incident()
    _advance_to_verifying(incident)
    bypassed = _verification().model_copy(update={"health_passed": 1})

    with pytest.raises(StateTransitionError, match="schema validation"):
        IncidentStateMachine().resolve_verified(
            incident, bypassed, reason="copy bypass must be rejected"
        )

    assert incident.status == IncidentStatus.VERIFYING


@pytest.mark.parametrize("requested_state", list(IncidentStatus))
def test_state_cannot_be_assigned_without_state_machine(requested_state) -> None:
    incident = _incident()

    with pytest.raises(ValueError, match="through IncidentStateMachine"):
        incident.status = requested_state


def test_gate_decision_updates_action_lifecycle_without_incident_transition() -> None:
    action = _action()
    assert apply_gate_decision(action, ActionDecision.ALLOW) == ActionStatus.GATED
    assert apply_gate_decision(action, ActionDecision.REQUIRE_APPROVAL) == ActionStatus.AWAITING_APPROVAL
    assert apply_gate_decision(action, ActionDecision.DENY) == ActionStatus.DENIED


def test_cross_references_accept_valid_objects() -> None:
    validate_pipeline_references(_incident(), _action(), [_evidence()], [_resource()])


@pytest.mark.parametrize(
    "action,evidence,resource,error",
    [
        (_action(incident_id="inc-other"), _evidence(), _resource(), "action incident_id"),
        (_action(), _evidence("inc-other"), _resource(), "another incident"),
        (_action(evidence_refs=["ev-missing"]), _evidence(), _resource(), "missing evidence"),
        (_action(target_resource_id="missing"), _evidence(), _resource(), "does not exist"),
        (_action(), _evidence(), _resource(Environment.PRODUCTION), "environment"),
    ],
)
def test_cross_reference_failures(
    action: TypedAction,
    evidence: Evidence,
    resource: Resource,
    error: str,
) -> None:
    with pytest.raises(ContractValidationError, match=error):
        validate_pipeline_references(_incident(), action, [evidence], [resource])


def test_existing_action_serialization_remains_compatible() -> None:
    action = _action()
    cloned = TypedAction(**deepcopy(action.model_dump()))
    assert cloned == action
    assert cloned.status == ActionStatus.PROPOSED
