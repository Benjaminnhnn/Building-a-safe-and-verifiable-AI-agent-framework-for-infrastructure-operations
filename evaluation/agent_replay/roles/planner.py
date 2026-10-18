"""Planner module — synthesizes ActionPlan domain proposals from Diagnosis and Evidence.

Design Invariants:
1. Strict Separation of Concerns: Planner ONLY proposes ActionPlan.
2. ZERO Execution Authority: Planner has NO execution capabilities and MUST NOT
   import any executor module (neither evaluation.agent_replay.domain.executor nor evaluation.agent_replay.roles.executor).
3. Produces a hash-stable, typed ActionPlan with explicit blast-radius and rollback.
4. Input and output envelopes propagate incident_id and evidence_ids.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.action_plan import (
    ActionPlan,
    BlastRadius,
    ExpectedScope,
    IrreversibleRollback,
    ReversibleRollback,
    Rollback,
)
from evaluation.agent_replay.domain.evidence import Evidence
from evaluation.agent_replay.domain.incident import Incident
from evaluation.agent_replay.roles.contracts import BaseRoleMessage
from evaluation.agent_replay.roles.diagnosis import DiagnosisOutput


class PlannerInput(BaseRoleMessage):
    """Input payload for the Planner role."""

    incident: Incident = Field(..., description="Active Incident model.")
    diagnosis: DiagnosisOutput = Field(..., description="Diagnosis result from Diagnosis role.")
    evidence_items: list[Evidence] = Field(
        default_factory=list,
        description="Evidence items supporting this plan.",
    )
    parameters: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Action execution parameters (no shell scripts allowed).",
    )
    blast_radius: BlastRadius = Field(
        default=BlastRadius.LOW,
        description="Declared maximum blast radius of the proposed action.",
    )
    reversible: bool = Field(
        default=True,
        description="Whether an automated rollback path is available.",
    )
    rollback_steps: list[str] = Field(
        default_factory=list,
        description="Ordered remediation steps to reverse the action if reversible.",
    )
    irreversible_reason: str | None = Field(
        default=None,
        description="Reason required when proposing an irreversible action.",
    )


class PlannerOutput(BaseRoleMessage):
    """Output envelope from the Planner role containing the proposed ActionPlan."""

    plan: ActionPlan = Field(..., description="Proposed ActionPlan domain model.")


class PlannerRole:
    """Planner component that formulates typed remediation plans.

    CRITICAL SAFETY GUARANTEE:
    This class and module do NOT import or invoke any executor. It has no
    capability to execute commands, apply changes, or bypass the Safety Gate.
    """

    def plan(self, input_data: PlannerInput) -> PlannerOutput:
        """Formulate a typed ActionPlan from the diagnosis without executing it."""
        if input_data.incident.incident_id != input_data.incident_id:
            raise ValueError("Incident ID mismatch between input and incident model")
        if input_data.diagnosis.incident_id != input_data.incident_id:
            raise ValueError("Incident ID mismatch between input and diagnosis output")
        if not input_data.diagnosis.ready_for_planning:
            raise ValueError(
                f"Diagnosis for incident {input_data.incident_id} is not ready for planning "
                f"(confidence={input_data.diagnosis.confidence})"
            )

        target_resource = input_data.diagnosis.target_resource_id or (
            input_data.incident.resource_ids[0] if input_data.incident.resource_ids else "unknown-target"
        )

        scope = ExpectedScope(
            affected_resource_ids=[target_resource],
            max_blast_radius=input_data.blast_radius,
        )

        rollback: Rollback
        if input_data.reversible:
            steps = input_data.rollback_steps or [
                f"revert_{input_data.diagnosis.recommended_action_type.value}_{target_resource}"
            ]
            rollback = ReversibleRollback(
                steps=steps,
                max_rollback_seconds=120,
            )
        else:
            reason = input_data.irreversible_reason or "Automated rollback unavailable for this action"
            rollback = IrreversibleRollback(reason=reason)

        evidence_ids = list(input_data.evidence_ids) or [e.evidence_id for e in input_data.evidence_items]
        plan_id = f"plan-{input_data.incident_id[:8]}-{input_data.diagnosis.cause_code}"

        preconditions = [
            "target resource exists",
        ]

        # ActionPlan computes plan_hash deterministically in its validator
        plan = ActionPlan(
            incident_id=input_data.incident_id,
            action_type=input_data.diagnosis.recommended_action_type,
            target_resource_id=target_resource,
            operation=f"{input_data.diagnosis.recommended_action_type.value} on {target_resource}",
            preconditions=preconditions,
            evidence_ids=evidence_ids,
            expected_scope=scope,
            expected_outcome=f"Remediate {input_data.diagnosis.root_cause}",
            rollback=rollback,
            requested_by="planner_agent",
            created_at=input_data.incident.created_at,
        )

        return PlannerOutput(
            incident_id=input_data.incident_id,
            evidence_ids=evidence_ids,
            plan=plan,
        )
