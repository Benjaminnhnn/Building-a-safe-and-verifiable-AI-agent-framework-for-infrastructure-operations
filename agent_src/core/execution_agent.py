"""Execution Agent — executes approved actions via typed adapters.

The Execution Agent:
- Only executes actions that received ALLOW from Safety Gate
- Routes actions to the correct adapter based on action_type
- Supports dry-run mode (no side effects)
- Captures pre/post snapshots for audit
- Provides rollback capability
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Protocol, runtime_checkable

from core.schema.action import TypedAction
from core.schema.common import ActionDecision, ActionStatus, ActionType
from core.schema.safety import SafetyDecision
from pydantic import BaseModel, Field


class ActionResult(BaseModel):
    """Result of executing an action."""
    action_id: str
    success: bool
    output: str
    duration_ms: float = 0.0
    pre_snapshot: dict[str, Any] = Field(default_factory=dict)
    post_snapshot: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True
    rollback_available: bool = False
    error: str | None = None


@runtime_checkable
class ActionAdapter(Protocol):
    """Protocol for action adapters."""
    
    def can_handle(self, action_type: ActionType) -> bool: ...
    
    def pre_check(self, action: TypedAction) -> dict[str, Any]: ...
    
    def execute(self, action: TypedAction, *, dry_run: bool = True) -> ActionResult: ...
    
    def rollback(self, action: TypedAction) -> ActionResult: ...


class DryRunAdapter:
    """Universal dry-run adapter that simulates any action type."""
    
    HANDLED_TYPES: ClassVar[set[ActionType]] = set(ActionType)
    
    def can_handle(self, action_type: ActionType) -> bool:
        return True
    
    def pre_check(self, action: TypedAction) -> dict[str, Any]:
        return {
            "status": "ok",
            "target_exists": True,
            "environment": action.environment.value,
            "action_type": action.action_type.value,
        }
    
    def execute(self, action: TypedAction, *, dry_run: bool = True) -> ActionResult:
        start = time.monotonic()
        output = (
            f"[DRY-RUN] Would execute {action.action_type.value} "
            f"on {action.target_resource_id} in {action.environment.value}"
        )
        elapsed = (time.monotonic() - start) * 1000
        return ActionResult(
            action_id=action.action_id,
            success=True,
            output=output,
            duration_ms=elapsed,
            pre_snapshot={"state": "simulated_before"},
            post_snapshot={"state": "simulated_after"},
            dry_run=True,
            rollback_available=action.rollback_plan.available,
        )
    
    def rollback(self, action: TypedAction) -> ActionResult:
        return ActionResult(
            action_id=action.action_id,
            success=True,
            output=f"[DRY-RUN] Rolled back {action.action_type.value}",
            dry_run=True,
            rollback_available=False,
        )


class ExecutionAgentError(Exception):
    pass


class ExecutionAgent:
    """Executes approved actions via typed adapters.
    
    Rules:
    - NEVER executes DENY or HUMAN_ONLY actions
    - REQUIRE_APPROVAL actions need explicit approval flag
    - Routes to correct adapter based on action_type
    - Always captures pre/post snapshot
    """
    
    def __init__(self, adapters: list[ActionAdapter] | None = None) -> None:
        self._adapters = adapters or [DryRunAdapter()]
    
    def execute(
        self,
        action: TypedAction,
        safety: SafetyDecision,
        *,
        dry_run: bool = True,
        approval_granted: bool = False,
    ) -> ActionResult:
        # Gate check: DENY and HUMAN_ONLY cannot proceed
        if safety.decision == ActionDecision.DENY:
            raise ExecutionAgentError(
                f"Cannot execute DENIED action {action.action_id}: {'; '.join(safety.reasons)}"
            )
        if safety.decision == ActionDecision.HUMAN_ONLY:
            raise ExecutionAgentError(
                f"Action {action.action_id} requires human-only execution"
            )
        
        # REQUIRE_APPROVAL needs explicit approval
        if safety.decision == ActionDecision.REQUIRE_APPROVAL and not approval_granted:
            raise ExecutionAgentError(
                f"Action {action.action_id} requires approval (not yet granted)"
            )
        
        # Find adapter
        adapter = self._find_adapter(action.action_type)
        if adapter is None:
            raise ExecutionAgentError(
                f"No adapter found for action type: {action.action_type.value}"
            )
        
        # Pre-check
        pre_check = adapter.pre_check(action)
        if pre_check.get("status") != "ok":
            return ActionResult(
                action_id=action.action_id,
                success=False,
                output=f"Pre-check failed: {pre_check}",
                dry_run=dry_run,
                error="pre_check_failed",
            )
        
        # Execute
        action.status = ActionStatus.EXECUTING
        result = adapter.execute(action, dry_run=dry_run)
        
        # Update action status
        if result.success:
            action.status = ActionStatus.SUCCEEDED
        else:
            action.status = ActionStatus.FAILED
        
        return result
    
    def rollback(self, action: TypedAction) -> ActionResult:
        adapter = self._find_adapter(action.action_type)
        if adapter is None:
            raise ExecutionAgentError(
                f"No adapter found for rollback: {action.action_type.value}"
            )
        return adapter.rollback(action)
    
    def _find_adapter(self, action_type: ActionType) -> ActionAdapter | None:
        for adapter in self._adapters:
            if adapter.can_handle(action_type):
                return adapter
        return None
