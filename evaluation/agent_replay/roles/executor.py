"""Executor module — wraps Phase 2c SafeExecutor into pipeline envelope.

Invariants:
1. CANNOT execute without a valid GatePass issued by SafetyGate.
2. Re-checks plan_hash, approval, and target state immediately before execution.
3. Fail-closed on any validation or execution mismatch.
4. Preserves incident_id and evidence_ids.
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.action_plan import ActionPlan
from evaluation.agent_replay.domain.approval import ApprovalRecord
from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.domain.executor import ExecutionResult, SafeExecutor
from evaluation.agent_replay.domain.gate_pass import GatePass
from evaluation.agent_replay.roles.contracts import BaseRoleMessage


class ExecutorInput(BaseRoleMessage):
    """Input payload for the Executor role."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plan: ActionPlan = Field(..., description="ActionPlan to execute.")
    gate_pass: GatePass = Field(..., description="Mandatory GatePass issued by SafetyGate.")
    target_client: Any = Field(..., description="Target execution adapter / client.")
    approval_record: ApprovalRecord | None = Field(
        default=None,
        description="Approval record if plan required approval.",
    )
    audit_logger: SafetyAuditLogger | None = Field(
        default=None,
        description="Optional audit logger.",
    )


class ExecutorOutput(BaseRoleMessage):
    """Output envelope from the Executor role."""

    plan_hash: str = Field(..., description="Canonical hash of the executed plan.")
    execution_result: ExecutionResult = Field(..., description="Detailed result of execution.")
    success: bool = Field(..., description="True if action completed without error.")
    error: str | None = Field(default=None, description="Error message if execution failed.")


class ExecutorRole:
    """Safe Executor component requiring a validated GatePass."""

    def __init__(self, executor: SafeExecutor | None = None) -> None:
        self.executor = executor or SafeExecutor()

    def execute(self, input_data: ExecutorInput) -> ExecutorOutput:
        if input_data.plan.incident_id != input_data.incident_id:
            raise ValueError("Plan incident_id mismatch with envelope incident_id")

        executor = self.executor
        if input_data.audit_logger:
            executor = SafeExecutor(audit_logger=input_data.audit_logger)

        try:
            result = executor.execute(
                plan=input_data.plan,
                gate_pass=input_data.gate_pass,
                client=input_data.target_client,
                approval=input_data.approval_record,
            )
            return ExecutorOutput(
                incident_id=input_data.incident_id,
                evidence_ids=input_data.evidence_ids,
                plan_hash=input_data.plan.plan_hash,
                execution_result=result,
                success=result.success,
                error=None,
            )
        except Exception as exc:
            dummy_result = ExecutionResult(
                success=False,
                plan_hash=input_data.plan.plan_hash,
                target_resource_id=input_data.plan.target_resource_id,
                action_type=input_data.plan.action_type.value,
                executed=False,
                output=str(exc),
            )
            return ExecutorOutput(
                incident_id=input_data.incident_id,
                evidence_ids=input_data.evidence_ids,
                plan_hash=input_data.plan.plan_hash,
                execution_result=dummy_result,
                success=False,
                error=str(exc),
            )
