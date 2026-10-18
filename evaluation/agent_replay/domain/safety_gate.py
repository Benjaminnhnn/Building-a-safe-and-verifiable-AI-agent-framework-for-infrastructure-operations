"""SafetyGate orchestrating Policy, Dry-Run, and Human Approval verification.

Invariants:
1. GatePass is only created when all 3 steps pass:
   - Step 1: Policy validation (blast radius, role permissions, forbidden pattern check)
   - Step 2: Dry-run execution (target resource exists, preconditions satisfied, diff preview generated)
   - Step 3: Approval verification (recompute plan_hash, check expiration, scope match, authorized approver)
2. Every decision and rejection is recorded into the append-only JSONL audit log.
3. GatePass constructor token (_SAFETY_GATE_TOKEN) is passed only by SafetyGate.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.action_plan import (
    ActionPlan,
    BlastRadius,
    IrreversibleRollback,
    _compute_plan_hash,
)
from evaluation.agent_replay.domain.approval import ApprovalRecord, ApprovalStore
from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.domain.dry_run import DryRunDecision, DryRunPreview, DryRunner, ReadOnlyTargetClient
from evaluation.agent_replay.domain.enums import ActionType
from evaluation.agent_replay.domain.gate_pass import GatePass, _SAFETY_GATE_TOKEN


class PolicyDecision(str, Enum):
    """Decision evaluated by the Safety Gate policy layer."""

    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"
    HUMAN_ONLY = "HUMAN_ONLY"


class SafetyGateResult(BaseModel):
    """Result of evaluating an ActionPlan through SafetyGate."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    permitted: bool
    policy_decision: PolicyDecision
    reason_code: str
    reason: str
    plan_hash: str
    preview: DryRunPreview | None = None
    gate_pass: Any = Field(default=None, description="GatePass instance if permitted, else None")


def format_diff_summary(preview: DryRunPreview | None) -> str:
    """Produce a concise, human-readable summary of projected diff changes."""
    if preview is None or preview.diff is None:
        return "no diff preview"
    changes = preview.diff.changes
    if not changes:
        return "no state modifications"
    parts = []
    for key, change in sorted(changes.items()):
        old_v = change.get("old")
        new_v = change.get("new")
        parts.append(f"{key}: {old_v} -> {new_v}")
    return "; ".join(parts)


class SafetyGate:
    """Enforces 3-step verification gate before issuing an unforgeable GatePass."""

    FORBIDDEN_OPERATION_PATTERNS = [
        re.compile(r"\bdrop\b", re.IGNORECASE),
        re.compile(r"\btruncate\b", re.IGNORECASE),
        re.compile(r"\bdelete[_\s]database\b", re.IGNORECASE),
        re.compile(r"\bdelete[_\s]volume\b", re.IGNORECASE),
        re.compile(r"\bunrestricted[_\s]shell\b", re.IGNORECASE),
        re.compile(r"\brm\s+-rf\s+/\b", re.IGNORECASE),
    ]

    def __init__(
        self,
        *,
        authorized_approvers: set[str],
        audit_logger: SafetyAuditLogger | None = None,
        approval_store: ApprovalStore | None = None,
        allowed_roles: set[str] | None = None,
    ) -> None:
        self._authorized_approvers = set(authorized_approvers)
        self._audit_logger = audit_logger or SafetyAuditLogger()
        self._approval_store = approval_store or ApprovalStore()
        self._allowed_roles = allowed_roles or {"operator", "planner_agent", "executor", "admin"}
        self._dry_runner = DryRunner()

    @property
    def audit_logger(self) -> SafetyAuditLogger:
        return self._audit_logger

    @property
    def approval_store(self) -> ApprovalStore:
        return self._approval_store

    def evaluate_policy(self, plan: ActionPlan, actor_role: str) -> tuple[PolicyDecision, str]:
        """Step 1: Determine initial policy decision for the proposed ActionPlan."""
        # Check permanently denied action types
        if plan.action_type is ActionType.BLOCKED_UNRESTRICTED_SHELL:
            return PolicyDecision.DENY, "BLOCKED_UNRESTRICTED_SHELL is permanently denied"

        # Check forbidden patterns in operation description
        for pat in self.FORBIDDEN_OPERATION_PATTERNS:
            if pat.search(plan.operation):
                return PolicyDecision.DENY, f"Operation contains forbidden pattern: {pat.pattern}"

        # Check role authorization
        if actor_role not in self._allowed_roles:
            return PolicyDecision.DENY, f"Role '{actor_role}' is not authorized to propose actions"

        # Check if human approval is mandatory
        if isinstance(plan.rollback, IrreversibleRollback):
            return PolicyDecision.REQUIRE_APPROVAL, "Irreversible action requires human approval"

        if plan.expected_scope.max_blast_radius in (BlastRadius.MEDIUM, BlastRadius.HIGH):
            return PolicyDecision.REQUIRE_APPROVAL, "Medium or high blast radius requires human approval"

        return PolicyDecision.ALLOW, "Low-risk reversible action permitted under standard policy"

    def evaluate_and_issue_pass(
        self,
        plan: ActionPlan,
        client: ReadOnlyTargetClient | Any,
        *,
        approval: ApprovalRecord | None = None,
        actor_role: str = "operator",
    ) -> SafetyGateResult:
        """Run all 3 verification steps. Returns SafetyGateResult with GatePass only if all pass."""
        if not isinstance(plan, ActionPlan):
            raise TypeError("plan must be an instance of ActionPlan")

        diff_summary = ""

        # --- STEP 1: Policy evaluation ---
        policy_decision, policy_reason = self.evaluate_policy(plan, actor_role)
        if policy_decision in (PolicyDecision.DENY, PolicyDecision.HUMAN_ONLY):
            reason_code = "FORBIDDEN_ACTION" if "forbidden" in policy_reason.lower() else "POLICY_DENY"
            self._audit_logger.log(
                plan_hash=plan.plan_hash,
                policy_decision=policy_decision.value,
                reason_code=reason_code,
                diff_summary="Policy rejected before dry-run",
                approver=None,
                result="REJECTED",
            )
            return SafetyGateResult(
                permitted=False,
                policy_decision=policy_decision,
                reason_code=reason_code,
                reason=policy_reason,
                plan_hash=plan.plan_hash,
                preview=None,
                gate_pass=None,
            )

        # --- STEP 2: Dry-Run evaluation ---
        dry_result = self._dry_runner.run(plan, client)
        if dry_result.preview:
            diff_summary = format_diff_summary(dry_result.preview)

        if dry_result.decision != DryRunDecision.ALLOW:
            reason_code = (
                "TARGET_NOT_FOUND"
                if "not found" in dry_result.reason.lower()
                else "PRECONDITION_FAILED"
            )
            self._audit_logger.log(
                plan_hash=plan.plan_hash,
                policy_decision=policy_decision.value,
                reason_code=reason_code,
                diff_summary=diff_summary or dry_result.reason,
                approver=None,
                result="REJECTED",
            )
            return SafetyGateResult(
                permitted=False,
                policy_decision=policy_decision,
                reason_code=reason_code,
                reason=dry_result.reason,
                plan_hash=plan.plan_hash,
                preview=dry_result.preview,
                gate_pass=None,
            )

        # --- STEP 3: Approval verification ---
        if policy_decision == PolicyDecision.REQUIRE_APPROVAL:
            # If approval not directly passed, look up from store
            if approval is None:
                approval = self._approval_store.get_latest_for_plan(plan.plan_hash)

            if approval is None:
                reason_code = "APPROVAL_REQUIRED"
                reason_msg = "Plan requires human approval but none was provided"
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision=policy_decision.value,
                    reason_code=reason_code,
                    diff_summary=diff_summary,
                    approver=None,
                    result="REJECTED",
                )
                return SafetyGateResult(
                    permitted=False,
                    policy_decision=policy_decision,
                    reason_code=reason_code,
                    reason=reason_msg,
                    plan_hash=plan.plan_hash,
                    preview=dry_result.preview,
                    gate_pass=None,
                )

            # Re-calculate plan_hash and compare with approval
            payload = plan.model_dump(mode="json", exclude={"plan_hash"})
            recalculated_hash = _compute_plan_hash(payload)

            if recalculated_hash != plan.plan_hash:
                reason_code = "PLAN_HASH_TAMPERED"
                reason_msg = "Plan was tampered: recomputed hash does not match plan.plan_hash"
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision=policy_decision.value,
                    reason_code=reason_code,
                    diff_summary=diff_summary,
                    approver=approval.approver,
                    result="REJECTED",
                )
                return SafetyGateResult(
                    permitted=False,
                    policy_decision=policy_decision,
                    reason_code=reason_code,
                    reason=reason_msg,
                    plan_hash=plan.plan_hash,
                    preview=dry_result.preview,
                    gate_pass=None,
                )

            if approval.plan_hash != plan.plan_hash:
                reason_code = "PLAN_HASH_MISMATCH"
                reason_msg = f"Approval plan_hash '{approval.plan_hash}' does not match plan '{plan.plan_hash}'"
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision=policy_decision.value,
                    reason_code=reason_code,
                    diff_summary=diff_summary,
                    approver=approval.approver,
                    result="REJECTED",
                )
                return SafetyGateResult(
                    permitted=False,
                    policy_decision=policy_decision,
                    reason_code=reason_code,
                    reason=reason_msg,
                    plan_hash=plan.plan_hash,
                    preview=dry_result.preview,
                    gate_pass=None,
                )

            if approval.is_expired():
                reason_code = "APPROVAL_EXPIRED"
                reason_msg = f"Approval expired at {approval.expires_at.isoformat()}"
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision=policy_decision.value,
                    reason_code=reason_code,
                    diff_summary=diff_summary,
                    approver=approval.approver,
                    result="REJECTED",
                )
                return SafetyGateResult(
                    permitted=False,
                    policy_decision=policy_decision,
                    reason_code=reason_code,
                    reason=reason_msg,
                    plan_hash=plan.plan_hash,
                    preview=dry_result.preview,
                    gate_pass=None,
                )

            # Scope check
            if not approval.scope.matches(plan.action_type, plan.target_resource_id):
                reason_code = "SCOPE_MISMATCH"
                reason_msg = (
                    f"Approval scope ({approval.scope.action_type.value}, {approval.scope.target}) "
                    f"does not match plan ({plan.action_type.value}, {plan.target_resource_id})"
                )
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision=policy_decision.value,
                    reason_code=reason_code,
                    diff_summary=diff_summary,
                    approver=approval.approver,
                    result="REJECTED",
                )
                return SafetyGateResult(
                    permitted=False,
                    policy_decision=policy_decision,
                    reason_code=reason_code,
                    reason=reason_msg,
                    plan_hash=plan.plan_hash,
                    preview=dry_result.preview,
                    gate_pass=None,
                )

            # Approver authorization check
            if approval.approver not in self._authorized_approvers:
                reason_code = "UNAUTHORIZED_APPROVER"
                reason_msg = f"Approver '{approval.approver}' is not in authorized approver list"
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision=policy_decision.value,
                    reason_code=reason_code,
                    diff_summary=diff_summary,
                    approver=approval.approver,
                    result="REJECTED",
                )
                return SafetyGateResult(
                    permitted=False,
                    policy_decision=policy_decision,
                    reason_code=reason_code,
                    reason=reason_msg,
                    plan_hash=plan.plan_hash,
                    preview=dry_result.preview,
                    gate_pass=None,
                )

        # --- ALL THREE STEPS PASSED: Create GatePass ---
        approver_name = approval.approver if approval else None
        gate_pass = GatePass(
            plan_hash=plan.plan_hash,
            target_resource_id=plan.target_resource_id,
            action_type=plan.action_type,
            issued_at=datetime.now(timezone.utc),
            approver=approver_name,
            _token=_SAFETY_GATE_TOKEN,
        )

        self._audit_logger.log(
            plan_hash=plan.plan_hash,
            policy_decision=policy_decision.value,
            reason_code="GATE_PASS_ISSUED",
            diff_summary=diff_summary,
            approver=approver_name,
            result="PERMITTED",
        )

        return SafetyGateResult(
            permitted=True,
            policy_decision=policy_decision,
            reason_code="GATE_PASS_ISSUED",
            reason="All 3 safety checks (policy, dry-run, approval) successfully verified",
            plan_hash=plan.plan_hash,
            preview=dry_result.preview,
            gate_pass=gate_pass,
        )

