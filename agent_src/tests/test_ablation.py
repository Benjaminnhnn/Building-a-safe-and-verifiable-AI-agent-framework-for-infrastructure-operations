from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.ablation import health_only_counterfactual, no_safety_gate_counterfactual
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionDecision, ActionType, Environment
from core.schema.safety import SafetyDecision
from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult


def _verification(*, health: bool, contract: bool) -> VerificationResult:
    checked_at = datetime.now(timezone.utc)
    return VerificationResult(
        verification_id="verify-ablation",
        incident_id="incident-ablation",
        health_passed=health,
        communication_contract_passed=contract,
        stability_seconds=120,
        stability_observations=[
            StabilityObservation(observed_at=checked_at - timedelta(seconds=120), healthy=health),
            StabilityObservation(observed_at=checked_at, healthy=health),
        ],
        allowed_probes=[ProbeResult(name="service_health", passed=health, details="health")],
        forbidden_probes=[ProbeResult(name="public_path", passed=contract, details="forbidden")],
        related_probes=[ProbeResult(name="related_service", passed=contract, details="related")],
        verdict="resolved" if health and contract else "not_resolved",
        resolution_eligible=health and contract,
    )


def test_health_only_ablation_records_false_recovery_without_canonical_authority() -> None:
    counterfactual = health_only_counterfactual(_verification(health=True, contract=False))

    assert counterfactual.health_only_would_resolve is True
    assert counterfactual.contract_satisfied is False
    assert counterfactual.false_recovery is True
    assert counterfactual.counterfactual_verdict == "would_resolve"
    assert counterfactual.counterfactual_only is True
    assert counterfactual.canonical_resolution_actor == "independent_verifier"


def test_health_only_ablation_does_not_resolve_when_health_fails() -> None:
    counterfactual = health_only_counterfactual(_verification(health=False, contract=True))

    assert counterfactual.health_only_would_resolve is False
    assert counterfactual.false_recovery is False
    assert counterfactual.counterfactual_verdict == "would_not_resolve"


def _action(action_id: str) -> TypedAction:
    return TypedAction(
        action_id=action_id,
        incident_id="incident-ablation",
        action_type=ActionType.RESTART_CONTAINER,
        target_resource_id="moodle-app",
        environment=Environment.STAGING,
        reason="bounded test action",
        evidence_refs=["evidence-1"],
        expected_outcome="service restarts",
        reversible=True,
        rollback_plan=RollbackPlan(available=False),
    )


def _decision(action: TypedAction, decision: ActionDecision) -> SafetyDecision:
    return SafetyDecision(
        decision_id=f"gate-{action.action_id}",
        action_id=action.action_id,
        action_hash=action.action_hash,
        incident_id=action.incident_id,
        decision=decision,
    )


def test_no_safety_gate_projection_intercepts_denied_proposals_without_execution() -> None:
    denied = _action("act-denied")
    approval = _action("act-approval")

    result = no_safety_gate_counterfactual(
        [denied, approval],
        [
            _decision(denied, ActionDecision.HUMAN_ONLY),
            _decision(approval, ActionDecision.REQUIRE_APPROVAL),
        ],
    )

    assert result.shadow_would_execute == ("act-denied",)
    assert result.dangerous_would_execute_count == 1
    assert result.sandbox_intercepted_action_count == 1
    assert result.sandbox_intercepted is True
    assert result.live_action_count == 0
    assert result.counterfactual_only is True
    assert len(result.action_plan_sha256) == 64


def test_no_safety_gate_projection_rejects_unbound_gate_decision() -> None:
    action = _action("act-denied")
    decision = _decision(action, ActionDecision.DENY).model_copy(
        update={"action_hash": "0" * 64}
    )

    with pytest.raises(ValueError, match="not bound"):
        no_safety_gate_counterfactual([action], [decision])
