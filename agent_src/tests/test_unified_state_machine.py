from __future__ import annotations

from copy import deepcopy

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


def test_full_valid_transition_path_requires_independent_verifier() -> None:
    incident = _incident()
    machine = IncidentStateMachine()
    path = [
        (IncidentStatus.TRIAGED, "observer"),
        (IncidentStatus.PLANNED, "planner"),
        (IncidentStatus.GATED, "safety_gate"),
        (IncidentStatus.EXECUTED, "executor"),
        (IncidentStatus.VERIFYING, "orchestrator"),
        (IncidentStatus.RESOLVED, "independent_verifier"),
    ]

    events = [
        machine.transition(incident, state, actor=actor, reason="test", evidence_refs=["ev-db-01"])
        for state, actor in path
    ]

    assert incident.status == IncidentStatus.RESOLVED
    assert incident.resolved_by_verifier is True
    assert [event.new_state for event in events] == [state for state, _ in path]


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
    incident.status = current

    with pytest.raises(StateTransitionError) as exc_info:
        IncidentStateMachine().transition(incident, requested, actor="planner", reason="invalid")

    assert exc_info.value.current_state == current
    assert exc_info.value.requested_state == requested
    assert exc_info.value.reason == "invalid"


@pytest.mark.parametrize("actor", ["planner", "executor", "orchestrator"])
def test_only_verifier_can_resolve(actor: str) -> None:
    incident = _incident()
    incident.status = IncidentStatus.VERIFYING
    with pytest.raises(StateTransitionError, match="only independent_verifier"):
        IncidentStateMachine().transition(
            incident,
            IncidentStatus.RESOLVED,
            actor=actor,
            reason="not authorized",
        )


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
