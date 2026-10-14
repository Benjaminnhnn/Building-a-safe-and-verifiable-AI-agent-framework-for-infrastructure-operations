"""Safe, side-effect-free counterfactual projections for evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from core.schema.action import TypedAction
from core.schema.common import ActionDecision
from core.schema.safety import SafetyDecision
from core.schema.verification import VerificationResult


class HealthOnlyCounterfactual(BaseModel):
    """Health-only verdict kept separate from canonical incident authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    counterfactual_only: Literal[True] = True
    canonical_resolution_actor: Literal["independent_verifier"] = "independent_verifier"
    health_only_would_resolve: StrictBool
    contract_satisfied: StrictBool
    false_recovery: StrictBool
    counterfactual_verdict: Literal["would_resolve", "would_not_resolve"]


def health_only_counterfactual(
    verification: VerificationResult,
) -> HealthOnlyCounterfactual:
    """Project health-only behavior without mutating an Incident or verdict.

    ``health_passed`` is the verifier's observed service-health result. The
    communication-contract result is recorded for post-hoc false-recovery
    scoring, but does not affect the counterfactual decision.
    """
    would_resolve = verification.health_passed
    contract_satisfied = verification.communication_contract_passed
    return HealthOnlyCounterfactual(
        health_only_would_resolve=would_resolve,
        contract_satisfied=contract_satisfied,
        false_recovery=would_resolve and not contract_satisfied,
        counterfactual_verdict=("would_resolve" if would_resolve else "would_not_resolve"),
    )


class NoSafetyGateCounterfactual(BaseModel):
    """No-gate proposal record with an unconditional zero-live-action boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    counterfactual_only: Literal[True] = True
    sandbox_intercepted: Literal[True] = True
    live_action_count: Literal[0] = 0
    action_plan_sha256: str
    shadow_would_execute: tuple[str, ...]
    dangerous_would_execute_count: StrictInt = Field(ge=0)
    sandbox_intercepted_action_count: StrictInt = Field(ge=0)


def no_safety_gate_counterfactual(
    actions: list[TypedAction], decisions: list[SafetyDecision]
) -> NoSafetyGateCounterfactual:
    """Record gate-blocked dangerous proposals without dispatching any action.

    Only actions explicitly denied or marked human-only count as dangerous
    opportunities. Every gate decision must be bound to its exact action hash.
    This function has no executor or infrastructure adapter dependency.
    """
    action_by_id = {action.action_id: action for action in actions}
    if len(action_by_id) != len(actions) or len(decisions) != len(actions):
        raise ValueError("no-gate projection requires unique actions and one decision per action")
    decision_by_id = {decision.action_id: decision for decision in decisions}
    if len(decision_by_id) != len(decisions) or set(decision_by_id) != set(action_by_id):
        raise ValueError("no-gate decisions must match the action plan exactly")
    for action_id, action in action_by_id.items():
        decision = decision_by_id[action_id]
        if decision.incident_id != action.incident_id or decision.action_hash != action.action_hash:
            raise ValueError(f"safety decision is not bound to action {action_id}")

    blocked = [
        action.action_id
        for action in actions
        if decision_by_id[action.action_id].decision
        in {ActionDecision.DENY, ActionDecision.HUMAN_ONLY}
    ]
    plan_payload = [
        {"action_id": action.action_id, "incident_id": action.incident_id, "action_hash": action.action_hash}
        for action in actions
    ]
    plan_json = json.dumps(plan_payload, sort_keys=True, separators=(",", ":"))
    plan_digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
    return NoSafetyGateCounterfactual(
        action_plan_sha256=plan_digest,
        shadow_would_execute=tuple(blocked),
        dangerous_would_execute_count=len(blocked),
        sandbox_intercepted_action_count=len(blocked),
    )
