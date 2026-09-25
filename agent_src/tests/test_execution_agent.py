"""Tests for the Execution Agent."""

import pytest
from core.execution_agent import (
    ExecutionAgent,
    ExecutionAgentError,
)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionDecision, ActionType, Environment
from core.schema.safety import SafetyDecision


def _make_action(**overrides) -> TypedAction:
    defaults = {
        "action_id": "act-test-001",
        "incident_id": "inc-test-001",
        "action_type": ActionType.START_CONTAINER,
        "target_resource_id": "postgres-db",
        "environment": Environment.STAGING,
        "reason": "test",
        "evidence_refs": ["ev-001"],
        "parameters": {},
        "preconditions": [],
        "expected_outcome": "recovery",
        "reversible": True,
        "rollback_plan": RollbackPlan(available=True, method="stop container"),
    }
    defaults.update(overrides)
    return TypedAction(**defaults)


def _make_safety(decision: ActionDecision = ActionDecision.ALLOW) -> SafetyDecision:
    return SafetyDecision(
        decision_id="gate-test",
        action_id="act-test-001",
        incident_id="inc-test-001",
        decision=decision,
        reasons=["test"],
    )


class TestExecutionAgent:
    def setup_method(self):
        self.agent = ExecutionAgent()

    def test_dry_run_success(self):
        action = _make_action()
        safety = _make_safety(ActionDecision.ALLOW)
        result = self.agent.execute(action, safety, dry_run=True)
        assert result.success
        assert result.dry_run
        assert "DRY-RUN" in result.output

    def test_deny_raises(self):
        action = _make_action()
        safety = _make_safety(ActionDecision.DENY)
        with pytest.raises(ExecutionAgentError, match="DENIED"):
            self.agent.execute(action, safety)

    def test_human_only_raises(self):
        action = _make_action()
        safety = _make_safety(ActionDecision.HUMAN_ONLY)
        with pytest.raises(ExecutionAgentError, match="human-only"):
            self.agent.execute(action, safety)

    def test_approval_required_without_grant_raises(self):
        action = _make_action()
        safety = _make_safety(ActionDecision.REQUIRE_APPROVAL)
        with pytest.raises(ExecutionAgentError, match="requires approval"):
            self.agent.execute(action, safety)

    def test_approval_required_with_grant_succeeds(self):
        action = _make_action()
        safety = _make_safety(ActionDecision.REQUIRE_APPROVAL)
        result = self.agent.execute(action, safety, approval_granted=True)
        assert result.success

    def test_rollback(self):
        action = _make_action()
        result = self.agent.rollback(action)
        assert result.success
        assert "Rolled back" in result.output
