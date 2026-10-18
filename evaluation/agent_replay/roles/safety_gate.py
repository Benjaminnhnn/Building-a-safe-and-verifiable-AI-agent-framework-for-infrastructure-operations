"""Safety Gate role module — wraps Phase 2c Safety Gate into pipeline envelope.

Invariants:
1. Evaluates ActionPlan through dry-run and policy verification.
2. Only generates a GatePass when all preconditions and policies (or approvals) pass.
3. Fail-closed: DENY or unapproved REQUIRE_APPROVAL yields None for gate_pass.
4. Preserves incident_id and evidence_ids.
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.action_plan import ActionPlan
from evaluation.agent_replay.domain.approval import ApprovalScope, ApprovalStore, HumanGate
from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.domain.dry_run import ReadOnlyTargetClient
from evaluation.agent_replay.domain.gate_pass import GatePass
from evaluation.agent_replay.domain.safety_gate import PolicyDecision, SafetyGate, SafetyGateResult
from evaluation.agent_replay.roles.contracts import BaseRoleMessage


class SafetyGateInput(BaseRoleMessage):
    """Input payload for the Safety Gate stage."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plan: ActionPlan = Field(..., description="ActionPlan proposed by Planner.")
    dry_run_client: ReadOnlyTargetClient = Field(..., description="Target client for dry-run simulation.")
    approval_store: ApprovalStore = Field(..., description="Append-only store for approvals.")
    approval_scope: ApprovalScope | None = Field(default=None, description="Approval scope if available.")
    actor: str = Field(default="safety_gate", description="Identity evaluating the gate.")
    human_gate: HumanGate | None = Field(default=None, description="Optional human gate notifier.")
    audit_logger: SafetyAuditLogger | None = Field(default=None, description="Audit logger.")


class SafetyGateOutput(BaseRoleMessage):
    """Output envelope from the Safety Gate stage."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    gate_result: SafetyGateResult | Any = Field(..., description="Underlying SafetyGate decision result.")
    decision: PolicyDecision = Field(..., description="Policy decision: ALLOW, REQUIRE_APPROVAL, or DENY.")
    gate_pass: GatePass | None = Field(default=None, description="GatePass value object if permitted.")
    reason_code: str = Field(..., description="Reason code explaining the gate verdict.")


class SafetyGateRole:
    """Safety Gate evaluator enforcing policy, dry-run diffs, and human approvals."""

    def __init__(
        self,
        gate: SafetyGate | None = None,
        authorized_approvers: set[str] | None = None,
        allowed_roles: set[str] | None = None,
    ) -> None:
        self.gate = gate or SafetyGate(
            authorized_approvers=authorized_approvers or {"admin", "infra-lead", "operator"},
            allowed_roles=allowed_roles or {"operator", "planner_agent", "executor", "admin", "pipeline_orchestrator"},
        )

    def evaluate(self, input_data: SafetyGateInput) -> SafetyGateOutput:
        if input_data.plan.incident_id != input_data.incident_id:
            raise ValueError("Plan incident_id mismatch with envelope incident_id")

        approval_record = None
        if input_data.approval_store:
            approval_record = input_data.approval_store.get_latest_for_plan(input_data.plan.plan_hash)

        result = self.gate.evaluate_and_issue_pass(
            plan=input_data.plan,
            client=input_data.dry_run_client,
            approval=approval_record,
            actor_role=input_data.actor,
        )

        decision = getattr(result, "policy_decision", getattr(result, "decision", PolicyDecision.DENY))

        return SafetyGateOutput(
            incident_id=input_data.incident_id,
            evidence_ids=input_data.evidence_ids,
            gate_result=result,
            decision=decision,
            gate_pass=result.gate_pass,
            reason_code=result.reason_code,
        )
