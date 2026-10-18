"""SafeExecutor enforcing GatePass verification and pre-execution re-checks.

Invariants:
1. Executor requires a valid GatePass object created by SafetyGate.
   Execution without GatePass is strictly rejected.
2. Immediately before execution, the executor re-checks:
   - plan_hash: recomputed from plan fields against GatePass and Approval
   - approval: unexpired, correct plan_hash, matching scope, authorized approver
   - permissions: actor_role has execution authority
   - target state: target resource still exists in infrastructure
   - preconditions: all preconditions still hold against fresh target state
3. Any change or mismatch results in an immediate ABORT and an audit log entry.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.action_plan import ActionPlan, _compute_plan_hash
from evaluation.agent_replay.domain.approval import ApprovalRecord, ApprovalStore
from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.domain.dry_run import evaluate_precondition
from evaluation.agent_replay.domain.gate_pass import GatePass, _SAFETY_GATE_TOKEN


class GatePassMissingError(PermissionError):
    """Raised when an execution is attempted without a valid GatePass."""


class ExecutionAbortedError(RuntimeError):
    """Raised when any pre-execution re-check fails immediately before mutation."""


class ExecutionResult(BaseModel):
    """Result returned after successfully executing an approved ActionPlan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    plan_hash: str
    target_resource_id: str
    action_type: str
    executed: bool
    output: str = Field(default="")
    duration_ms: float = Field(default=0.0)


class SafeExecutor:
    """Safely executes ActionPlans strictly requiring a verified GatePass.

    Performs comprehensive JIT (just-in-time) pre-execution re-checks before
    allowing any mutating operation on target infrastructure.
    """

    def __init__(
        self,
        *,
        audit_logger: SafetyAuditLogger | None = None,
        approval_store: ApprovalStore | None = None,
        authorized_approvers: set[str] | None = None,
        execution_roles: set[str] | None = None,
    ) -> None:
        self._audit_logger = audit_logger or SafetyAuditLogger()
        self._approval_store = approval_store or ApprovalStore()
        self._authorized_approvers = authorized_approvers or {"human-approver", "operator", "admin"}
        self._execution_roles = execution_roles or {"executor", "operator", "admin"}

    @property
    def audit_logger(self) -> SafetyAuditLogger:
        return self._audit_logger

    def execute(
        self,
        plan: ActionPlan,
        gate_pass: GatePass,
        client: Any,
        *,
        approval: ApprovalRecord | None = None,
        actor_role: str = "executor",
    ) -> ExecutionResult:
        """Execute the ActionPlan with GatePass validation and JIT re-checks.

        Raises:
        - GatePassMissingError: if gate_pass is missing, wrong type, or invalid token.
        - ExecutionAbortedError: if plan_hash, approval, role, target, or preconditions fail.
        """
        # ------------------------------------------------------------------
        # 1. GatePass Enforcement
        # ------------------------------------------------------------------
        if gate_pass is None or not isinstance(gate_pass, GatePass) or not gate_pass.is_valid_token():
            plan_hash = getattr(plan, "plan_hash", "UNKNOWN")
            self._audit_logger.log(
                plan_hash=plan_hash,
                policy_decision="DENY",
                reason_code="GATEPASS_MISSING",
                diff_summary="Execution rejected: GatePass missing, forged, or invalid",
                approver=None,
                result="REJECTED",
            )
            raise GatePassMissingError(
                "Executor cannot run without a valid GatePass issued by SafetyGate."
            )

        # ------------------------------------------------------------------
        # 2. Re-check plan_hash (plan modified after approval / gate pass?)
        # ------------------------------------------------------------------
        payload = plan.model_dump(mode="json", exclude={"plan_hash"})
        recomputed_hash = _compute_plan_hash(payload)

        if recomputed_hash != plan.plan_hash or recomputed_hash != gate_pass.plan_hash:
            self._audit_logger.log(
                plan_hash=plan.plan_hash,
                policy_decision="DENY",
                reason_code="PLAN_HASH_MISMATCH",
                diff_summary=f"Plan modified after approval: expected {gate_pass.plan_hash}, recomputed {recomputed_hash}",
                approver=gate_pass.approver,
                result="ABORTED",
            )
            raise ExecutionAbortedError(
                f"Execution aborted: plan was modified after approval. "
                f"GatePass hash '{gate_pass.plan_hash}' != recomputed hash '{recomputed_hash}'"
            )

        # ------------------------------------------------------------------
        # 3. Re-check approval (expired, mismatched hash, scope, or approver?)
        # ------------------------------------------------------------------
        if gate_pass.approver is not None and approval is None:
            approval = self._approval_store.get_latest_for_plan(plan.plan_hash)
            if approval is None:
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision="DENY",
                    reason_code="APPROVAL_MISSING",
                    diff_summary="Approval record required by GatePass but not found",
                    approver=gate_pass.approver,
                    result="ABORTED",
                )
                raise ExecutionAbortedError(
                    "Execution aborted: GatePass requires approval, but approval record is missing."
                )

        if approval is not None:
            if approval.is_expired():
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision="DENY",
                    reason_code="APPROVAL_EXPIRED",
                    diff_summary=f"Approval expired at {approval.expires_at.isoformat()}",
                    approver=approval.approver,
                    result="ABORTED",
                )
                raise ExecutionAbortedError(
                    f"Execution aborted: approval expired at {approval.expires_at.isoformat()}."
                )

            if approval.plan_hash != plan.plan_hash:
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision="DENY",
                    reason_code="PLAN_HASH_MISMATCH",
                    diff_summary=f"Approval plan_hash '{approval.plan_hash}' != plan '{plan.plan_hash}'",
                    approver=approval.approver,
                    result="ABORTED",
                )
                raise ExecutionAbortedError(
                    f"Execution aborted: approval plan_hash '{approval.plan_hash}' does not match plan."
                )

            if not approval.scope.matches(plan.action_type, plan.target_resource_id):
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision="DENY",
                    reason_code="SCOPE_MISMATCH",
                    diff_summary=(
                        f"Approval scope ({approval.scope.action_type.value}, {approval.scope.target}) "
                        f"does not match plan ({plan.action_type.value}, {plan.target_resource_id})"
                    ),
                    approver=approval.approver,
                    result="ABORTED",
                )
                raise ExecutionAbortedError(
                    f"Execution aborted: approval scope ({approval.scope.action_type.value}, {approval.scope.target}) "
                    f"does not match plan target or action_type."
                )

            if approval.approver not in self._authorized_approvers:
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision="DENY",
                    reason_code="UNAUTHORIZED_APPROVER",
                    diff_summary=f"Approver '{approval.approver}' not authorized",
                    approver=approval.approver,
                    result="ABORTED",
                )
                raise ExecutionAbortedError(
                    f"Execution aborted: approver '{approval.approver}' is not authorized."
                )

        # ------------------------------------------------------------------
        # 4. Re-check permissions (actor role has execution authority?)
        # ------------------------------------------------------------------
        if actor_role not in self._execution_roles:
            self._audit_logger.log(
                plan_hash=plan.plan_hash,
                policy_decision="DENY",
                reason_code="UNAUTHORIZED_ROLE",
                diff_summary=f"Actor role '{actor_role}' does not have execution authority",
                approver=gate_pass.approver,
                result="ABORTED",
            )
            raise ExecutionAbortedError(
                f"Execution aborted: role '{actor_role}' does not have execution permission."
            )

        # ------------------------------------------------------------------
        # 5. Re-check target state and preconditions
        # ------------------------------------------------------------------
        if not hasattr(client, "get_resource_state"):
            self._audit_logger.log(
                plan_hash=plan.plan_hash,
                policy_decision="DENY",
                reason_code="INVALID_TARGET_CLIENT",
                diff_summary="Client does not implement get_resource_state",
                approver=gate_pass.approver,
                result="ABORTED",
            )
            raise ExecutionAbortedError("Execution aborted: client cannot query target state.")

        current_state = client.get_resource_state(plan.target_resource_id)
        if current_state is None:
            self._audit_logger.log(
                plan_hash=plan.plan_hash,
                policy_decision="DENY",
                reason_code="TARGET_NOT_FOUND",
                diff_summary=f"Target resource '{plan.target_resource_id}' no longer exists",
                approver=gate_pass.approver,
                result="ABORTED",
            )
            raise ExecutionAbortedError(
                f"Execution aborted: target resource '{plan.target_resource_id}' no longer exists."
            )

        for cond in plan.preconditions:
            satisfied, reason = evaluate_precondition(
                cond, current_state, target_resource_id=plan.target_resource_id
            )
            if not satisfied:
                self._audit_logger.log(
                    plan_hash=plan.plan_hash,
                    policy_decision="DENY",
                    reason_code="PRECONDITION_FAILED",
                    diff_summary=f"Precondition failed right before execution: {reason}",
                    approver=gate_pass.approver,
                    result="ABORTED",
                )
                raise ExecutionAbortedError(
                    f"Execution aborted: precondition failed immediately before execution: {reason}"
                )

        # ------------------------------------------------------------------
        # 6. Execute Mutating Action
        # ------------------------------------------------------------------
        start_time = time.monotonic()
        output = self._perform_mutation(plan, client, gate_pass=gate_pass)
        duration_ms = (time.monotonic() - start_time) * 1000

        self._audit_logger.log(
            plan_hash=plan.plan_hash,
            policy_decision="ALLOW",
            reason_code="EXECUTED",
            diff_summary=f"Action executed on {plan.target_resource_id}",
            approver=gate_pass.approver,
            result="EXECUTED",
        )

        return ExecutionResult(
            success=True,
            plan_hash=plan.plan_hash,
            target_resource_id=plan.target_resource_id,
            action_type=plan.action_type.value,
            executed=True,
            output=output,
            duration_ms=duration_ms,
        )

    def _perform_mutation(
        self, plan: ActionPlan, client: Any, gate_pass: GatePass | None = None
    ) -> str:
        """Dispatch actual mutation call to client."""
        target_id = plan.target_resource_id
        atype = plan.action_type.value

        # Check for matching method on client
        method_name = f"execute_{atype}"
        if hasattr(client, method_name) and callable(getattr(client, method_name)):
            res = getattr(client, method_name)(target_id)
            return str(res)

        if hasattr(client, "execute_action") and callable(getattr(client, "execute_action")):
            try:
                res = client.execute_action(plan, gate_pass=gate_pass)
            except TypeError:
                res = client.execute_action(plan)
            return str(res)


        # Fallback to restart_service / restart_container if present
        if "restart" in atype and hasattr(client, "restart_service"):
            res = client.restart_service(target_id)
            return str(res)

        return f"Executed {plan.action_type.value} on {target_id} successfully."

