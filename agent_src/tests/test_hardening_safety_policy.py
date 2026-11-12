"""Regression tests: the policy engine fails closed on the audit's fail-open inputs."""

from __future__ import annotations

import pytest

from core.safety.safety_policy_engine import BlastRadius, PolicyDecision, SafetyPolicyEngine


def _evaluate(**overrides):
    fields = dict(
        action="restart_service", environment="staging", blast_radius=BlastRadius.LOW,
        confidence=0.9, evidence_count=3, rollback_ready=True, actor_role="executor", is_reversible=True,
    )
    fields.update(overrides)
    return SafetyPolicyEngine.load_default_rules().evaluate(**fields)[0]


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.5, True])
def test_invalid_confidence_is_denied(confidence) -> None:
    assert _evaluate(confidence=confidence) == PolicyDecision.DENY


def test_low_confidence_production_is_denied_not_sent_for_approval() -> None:
    assert _evaluate(environment="production", confidence=0.1) == PolicyDecision.DENY


def test_unknown_environment_is_denied() -> None:
    assert _evaluate(environment="prod") == PolicyDecision.DENY


def test_read_only_name_prefix_does_not_allow_high_blast_radius() -> None:
    decision = _evaluate(action="check_x", environment="production", blast_radius=BlastRadius.HIGH,
                         confidence=0.9, actor_role="observer")
    assert decision != PolicyDecision.ALLOW
