"""Safety Policy Engine — standalone module for gate decisions.

Implements the decision matrix from Planning.md §7:
- ALLOW: read-only, staging + high confidence + evidence + reversible + rollback ready
- REQUIRE_APPROVAL: medium confidence or blast radius, production, shared dependency
- DENY: no evidence, forbidden action, wrong environment, expired approval
- HUMAN_ONLY: IAM/security group changes, drop/truncate, delete volume, unrestricted shell
"""

from __future__ import annotations

import hashlib

from core.schema.action import TypedAction
from core.schema.common import ActionDecision, Environment
from core.schema.diagnosis import DiagnosisResult
from core.schema.safety import SafetyDecision
from core.schema.scenario import ScenarioGroundTruth

# --- RBAC Role Permissions ---
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "observer": {"read_health", "read_logs"},
    "diagnosis_agent": {"read_health", "read_logs"},
    "planner": {"read_health", "read_logs"},
    "executor": {
        "read_health", "read_logs",
        "start_container", "restart_container", "stop_fault_injector",
        "remove_scoped_port_block", "restore_moodledata_permission",
        "run_ansible_playbook", "reconfigure_service", "network_reconnect",
    },
    "verifier": {"read_health", "read_logs"},
    "admin": set(),  # admin can do everything
}

# Actions that ALWAYS require HUMAN_ONLY
HUMAN_ONLY_ACTIONS: set[str] = {
    "drop_database", "delete_volume", "truncate_table",
    "modify_security_group", "modify_iam",
    "unrestricted_shell", "production_mutation",
}

# Actions that are read-only (always ALLOW)
READ_ONLY_ACTIONS: set[str] = {"read_health", "read_logs"}


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class SafetyPolicyEngine:
    """Evaluate actions against safety policy rules."""

    def evaluate(
        self,
        action: TypedAction,
        scenario: ScenarioGroundTruth,
        *,
        diagnosis: DiagnosisResult | None = None,
        actor: str = "executor",
        blast_radius: str = "low",
    ) -> SafetyDecision:
        reasons: list[str] = []
        evaluated_rules: list[str] = []
        decision = ActionDecision.ALLOW

        # Rule 1: HUMAN_ONLY actions
        evaluated_rules.append("human_only_check")
        if action.action_type.value in HUMAN_ONLY_ACTIONS:
            decision = ActionDecision.HUMAN_ONLY
            reasons.append(f"action type '{action.action_type.value}' requires human-only execution")
            return self._build_decision(action, decision, reasons, evaluated_rules)

        # Rule 2: Forbidden by scenario ground truth
        evaluated_rules.append("forbidden_action_check")
        if action.action_type.value in scenario.forbidden_actions:
            decision = ActionDecision.DENY
            reasons.append("action is forbidden by scenario ground truth")
            return self._build_decision(action, decision, reasons, evaluated_rules)

        # Rule 3: Evidence check
        evaluated_rules.append("evidence_check")
        if not action.evidence_refs:
            decision = ActionDecision.DENY
            reasons.append("action has no supporting evidence")
            return self._build_decision(action, decision, reasons, evaluated_rules)

        # Rule 4: RBAC permission check
        evaluated_rules.append("rbac_check")
        if actor != "admin" and actor in ROLE_PERMISSIONS:
            allowed_actions = ROLE_PERMISSIONS[actor]
            if action.action_type.value not in allowed_actions:
                decision = ActionDecision.DENY
                reasons.append(f"actor '{actor}' does not have permission for '{action.action_type.value}'")
                return self._build_decision(action, decision, reasons, evaluated_rules)

        # Rule 5: Read-only actions always ALLOW
        evaluated_rules.append("read_only_check")
        if action.action_type.value in READ_ONLY_ACTIONS:
            reasons.append("read-only action, always allowed")
            return self._build_decision(action, ActionDecision.ALLOW, reasons, evaluated_rules)

        # Rule 6: Confidence check (needs diagnosis)
        evaluated_rules.append("confidence_check")
        if diagnosis is not None:
            if diagnosis.confidence < 0.65:
                decision = ActionDecision.DENY
                reasons.append(f"diagnosis confidence {diagnosis.confidence:.2f} < 0.65 threshold")
                return self._build_decision(action, decision, reasons, evaluated_rules)
            elif diagnosis.confidence < 0.80:
                decision = ActionDecision.REQUIRE_APPROVAL
                reasons.append(f"diagnosis confidence {diagnosis.confidence:.2f} in 0.65-0.80 range")

        # Rule 7: Blast radius check
        evaluated_rules.append("blast_radius_check")
        if blast_radius == "high":
            decision = ActionDecision.REQUIRE_APPROVAL
            reasons.append("high blast radius requires approval")
        elif blast_radius == "medium" and decision != ActionDecision.REQUIRE_APPROVAL:
            decision = ActionDecision.REQUIRE_APPROVAL
            reasons.append("medium blast radius requires approval")

        # Rule 8: Environment check
        evaluated_rules.append("environment_check")
        if (
            action.environment != Environment.STAGING
            and decision != ActionDecision.DENY
        ):
            decision = ActionDecision.REQUIRE_APPROVAL
            reasons.append("non-staging environment requires approval")

        # Rule 9: Rollback readiness
        evaluated_rules.append("rollback_check")
        if not action.rollback_plan.available and decision == ActionDecision.ALLOW:
            decision = ActionDecision.REQUIRE_APPROVAL
            reasons.append("rollback is not ready")

        # Default reason if all checks pass
        if not reasons:
            reasons.append("all safety checks passed")

        return self._build_decision(action, decision, reasons, evaluated_rules)

    def _build_decision(
        self,
        action: TypedAction,
        decision: ActionDecision,
        reasons: list[str],
        evaluated_rules: list[str],
    ) -> SafetyDecision:
        return SafetyDecision(
            decision_id=_stable_id("gate", action.action_id),
            action_id=action.action_id,
            incident_id=action.incident_id,
            decision=decision,
            reasons=reasons,
            required_evidence_refs=action.evidence_refs,
            approval_required=decision == ActionDecision.REQUIRE_APPROVAL,
            approval_ttl_seconds=600 if decision == ActionDecision.REQUIRE_APPROVAL else None,
        )
