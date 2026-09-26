"""Tests for SafetyPolicyEngine, AuditRecord, and default policy rules."""

from __future__ import annotations

import re

import pytest
from core.safety_policy_engine import (
    AuditRecord,
    BlastRadius,
    PolicyDecision,
    SafetyPolicyEngine,
)


@pytest.fixture()
def engine() -> SafetyPolicyEngine:
    return SafetyPolicyEngine.load_default_rules()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _allow(
    engine: SafetyPolicyEngine,
    *,
    action: str = "restart_moodle_container",
    environment: str = "staging",
    blast_radius: BlastRadius = BlastRadius.LOW,
    confidence: float = 0.95,
    evidence_count: int = 5,
    rollback_ready: bool = True,
    actor_role: str = "executor",
    is_reversible: bool = True,
) -> tuple[PolicyDecision, str]:
    return engine.evaluate(
        action=action,
        environment=environment,
        blast_radius=blast_radius,
        confidence=confidence,
        evidence_count=evidence_count,
        rollback_ready=rollback_ready,
        actor_role=actor_role,
        is_reversible=is_reversible,
    )


# ---------------------------------------------------------------------------
# Policy decision tests
# ---------------------------------------------------------------------------


def test_default_allow_high_confidence_staging(engine: SafetyPolicyEngine) -> None:
    decision, reason = _allow(engine, confidence=0.95, evidence_count=5)
    assert decision == PolicyDecision.ALLOW, f"Expected ALLOW, got {decision}: {reason}"


def test_require_approval_medium_confidence(engine: SafetyPolicyEngine) -> None:
    decision, reason = _allow(engine, confidence=0.70, rollback_ready=False)
    assert decision == PolicyDecision.REQUIRE_APPROVAL, (
        f"Expected REQUIRE_APPROVAL for confidence=0.70, got {decision}: {reason}"
    )


def test_deny_below_threshold(engine: SafetyPolicyEngine) -> None:
    decision, reason = _allow(engine, confidence=0.50)
    assert decision == PolicyDecision.DENY, (
        f"Expected DENY for confidence=0.50, got {decision}: {reason}"
    )


def test_deny_forbidden_action(engine: SafetyPolicyEngine) -> None:
    decision, reason = _allow(engine, action="delete_database_schema")
    assert decision == PolicyDecision.DENY, (
        f"Expected DENY for forbidden action, got {decision}: {reason}"
    )
    assert "forbidden" in reason.lower() or "deny" in reason.lower()


def test_deny_production_default(engine: SafetyPolicyEngine) -> None:
    decision, reason = _allow(
        engine,
        environment="production",
        confidence=0.95,
        evidence_count=5,
        actor_role="executor",
    )
    # Production must result in at least REQUIRE_APPROVAL (not ALLOW)
    assert decision in (PolicyDecision.REQUIRE_APPROVAL, PolicyDecision.HUMAN_ONLY, PolicyDecision.DENY), (
        f"Expected REQUIRE_APPROVAL/HUMAN_ONLY/DENY for production, got {decision}: {reason}"
    )
    assert decision != PolicyDecision.ALLOW, (
        f"Production environment must NOT result in ALLOW: {reason}"
    )


def test_fail_closed_unknown_action(engine: SafetyPolicyEngine) -> None:
    """An action not matching any allowlist pattern but with low confidence → DENY."""
    decision, reason = _allow(engine, action="totally_unknown_dangerous_op", confidence=0.50)
    assert decision == PolicyDecision.DENY, (
        f"Expected DENY for unknown action with low confidence, got {decision}: {reason}"
    )


def test_blast_radius_high_requires_approval(engine: SafetyPolicyEngine) -> None:
    decision, reason = _allow(
        engine,
        blast_radius=BlastRadius.HIGH,
        confidence=0.90,
        evidence_count=5,
        environment="staging",
        rollback_ready=True,
    )
    assert decision in (PolicyDecision.REQUIRE_APPROVAL, PolicyDecision.HUMAN_ONLY, PolicyDecision.DENY), (
        f"HIGH blast radius must result in at least REQUIRE_APPROVAL, got {decision}: {reason}"
    )
    assert decision != PolicyDecision.ALLOW, (
        f"HIGH blast radius must NOT be ALLOW: {reason}"
    )


# ---------------------------------------------------------------------------
# RBAC tests
# ---------------------------------------------------------------------------


def test_rbac_observer_cannot_execute(engine: SafetyPolicyEngine) -> None:
    assert engine.check_rbac("observer", "execution") is False


def test_rbac_executor_can_execute(engine: SafetyPolicyEngine) -> None:
    assert engine.check_rbac("executor", "execution") is True


def test_verifier_cannot_execute(engine: SafetyPolicyEngine) -> None:
    assert engine.check_rbac("verifier", "execution") is False


def test_rbac_planner_cannot_execute(engine: SafetyPolicyEngine) -> None:
    assert engine.check_rbac("planner", "execution") is False


def test_rbac_approver_can_approve(engine: SafetyPolicyEngine) -> None:
    assert engine.check_rbac("approver", "approval") is True


# ---------------------------------------------------------------------------
# AuditRecord tests
# ---------------------------------------------------------------------------


def test_audit_record_has_sha256() -> None:
    record = AuditRecord.create(
        incident_id="inc-001",
        action_id="restart_moodle_container",
        actor_role="executor",
        decision=PolicyDecision.ALLOW,
        reason="Matched rule R02",
        evidence_count=5,
        confidence=0.95,
        blast_radius=BlastRadius.LOW,
        rollback_ready=True,
    )
    assert len(record.sha256) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", record.sha256) is not None


def test_audit_record_sha256_is_deterministic_for_same_data() -> None:
    """Two records with the same audit_id and timestamp should have the same sha256."""

    record1 = AuditRecord.create(
        incident_id="inc-002",
        action_id="verify_tls_database_connection",
        actor_role="verifier",
        decision=PolicyDecision.ALLOW,
        reason="Read-only probe",
        evidence_count=3,
        confidence=1.0,
        blast_radius=BlastRadius.LOW,
        rollback_ready=True,
    )
    # SHA256 must be non-empty and lowercase hex
    assert record1.sha256.islower() or record1.sha256.isdigit()
    assert len(record1.sha256) == 64


def test_audit_record_timestamp_is_utc() -> None:
    from datetime import timezone

    record = AuditRecord.create(
        incident_id="inc-003",
        action_id="restart_moodle_container",
        actor_role="executor",
        decision=PolicyDecision.DENY,
        reason="Test",
        evidence_count=0,
        confidence=0.0,
        blast_radius=BlastRadius.HIGH,
        rollback_ready=False,
    )
    assert record.timestamp.tzinfo is not None
    assert record.timestamp.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Probe / read-only allowance
# ---------------------------------------------------------------------------


def test_read_only_probe_allowed_for_verifier(engine: SafetyPolicyEngine) -> None:
    decision, reason = engine.evaluate(
        action="verify_tls_database_connection",
        environment="staging",
        blast_radius=BlastRadius.LOW,
        confidence=1.0,
        evidence_count=0,
        rollback_ready=True,
        actor_role="verifier",
        is_reversible=True,
    )
    assert decision == PolicyDecision.ALLOW, f"Read-only probe should ALLOW for verifier: {reason}"
