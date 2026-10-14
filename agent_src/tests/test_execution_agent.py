"""Tests for the Execution Agent."""

import pytest
from core.execution_agent import (
    ActionResult,
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


def _make_safety(
    action: TypedAction,
    decision: ActionDecision = ActionDecision.ALLOW,
    *,
    action_id: str | None = None,
    action_hash: str | None = None,
    incident_id: str | None = None,
) -> SafetyDecision:
    return SafetyDecision(
        decision_id="gate-test",
        action_id=action_id or action.action_id,
        action_hash=action_hash or action.action_hash,
        incident_id=incident_id or action.incident_id,
        decision=decision,
        reasons=["test"],
        required_evidence_refs=action.evidence_refs,
    )


class TestExecutionAgent:
    def setup_method(self):
        self.agent = ExecutionAgent()

    def test_dry_run_success(self):
        action = _make_action()
        safety = _make_safety(action, ActionDecision.ALLOW)
        result = self.agent.execute(action, safety, dry_run=True)
        assert result.success
        assert result.dry_run
        assert "DRY-RUN" in result.output

    def test_deny_raises(self):
        action = _make_action()
        safety = _make_safety(action, ActionDecision.DENY)
        with pytest.raises(ExecutionAgentError, match="DENIED"):
            self.agent.execute(action, safety)

    def test_human_only_raises(self):
        action = _make_action()
        safety = _make_safety(action, ActionDecision.HUMAN_ONLY)
        with pytest.raises(ExecutionAgentError, match="human-only"):
            self.agent.execute(action, safety)

    def test_approval_required_without_grant_raises(self):
        action = _make_action()
        safety = _make_safety(action, ActionDecision.REQUIRE_APPROVAL)
        with pytest.raises(ExecutionAgentError, match="requires approval"):
            self.agent.execute(action, safety)

    def test_approval_required_with_grant_succeeds(self):
        action = _make_action()
        safety = _make_safety(action, ActionDecision.REQUIRE_APPROVAL)
        result = self.agent.execute(action, safety, approval_granted=True)
        assert result.success

    def test_rollback(self):
        action = _make_action()
        result = self.agent.rollback(action)
        assert result.success
        assert result.dry_run
        assert "Would roll back" in result.output

    def test_rollback_rejects_unavailable_or_live_capability(self):
        unavailable = _make_action(
            reversible=False,
            rollback_plan=RollbackPlan(available=False),
        )
        with pytest.raises(ExecutionAgentError, match="no declared"):
            self.agent.rollback(unavailable)

        with pytest.raises(ExecutionAgentError, match="Safe Executor"):
            self.agent.rollback(_make_action(), dry_run=False)

    def test_action_level_approval_cannot_be_skipped_by_an_allow_decision(self):
        action = _make_action(requires_approval=True)
        safety = _make_safety(action, ActionDecision.ALLOW)

        with pytest.raises(ExecutionAgentError, match="requires approval"):
            self.agent.execute(action, safety)

    def test_gate_decision_cannot_be_reused_for_another_action(self):
        action = _make_action()
        other_action = _make_action(action_id="act-other")
        safety = _make_safety(action)

        with pytest.raises(ExecutionAgentError, match="does not match"):
            self.agent.execute(other_action, safety)

    def test_gate_decision_rejects_action_mutated_after_evaluation(self):
        action = _make_action()
        safety = _make_safety(action)
        action.parameters["container_name"] = "unreviewed-container"

        with pytest.raises(ExecutionAgentError, match="changed action"):
            self.agent.execute(action, safety)

    def test_execution_revalidates_action_and_gate_result_before_dispatch(self):
        malformed_action = _make_action().model_copy(update={"reversible": 1})
        malformed_safety = _make_safety(_make_action())

        with pytest.raises(ExecutionAgentError, match="schema validation"):
            self.agent.execute(malformed_action, malformed_safety)

        action = _make_action()
        malformed_safety = _make_safety(action).model_copy(
            update={"approval_required": 1}
        )
        with pytest.raises(ExecutionAgentError, match="schema validation"):
            self.agent.execute(action, malformed_safety)

    def test_adapter_result_must_match_action_and_requested_mode(self):
        class MismatchedAdapter:
            def can_handle(self, action_type):
                return True

            def pre_check(self, action):
                return {"status": "ok"}

            def execute(self, action, *, dry_run=True):
                return ActionResult(
                    action_id="another-action",
                    success=True,
                    output="ok",
                    dry_run=dry_run,
                )

            def rollback(self, action):
                raise AssertionError("rollback is not part of this test")

        agent = ExecutionAgent(adapters=[MismatchedAdapter()])
        action = _make_action()
        with pytest.raises(ExecutionAgentError, match="does not match the dispatched action"):
            agent.execute(action, _make_safety(action))

    def test_adapter_result_secrets_are_redacted_before_return(self):
        class LeakyAdapter:
            def can_handle(self, action_type):
                return True

            def pre_check(self, action):
                return {"status": "ok"}

            def execute(self, action, *, dry_run=True):
                return ActionResult(
                    action_id=action.action_id,
                    success=True,
                    output="db_password=hunter2",
                    pre_snapshot={"db_password": "hunter2", "health": "ok"},
                    post_snapshot={"client_secret": "fixture-secret", "health": "ok"},
                    dry_run=dry_run,
                )

            def rollback(self, action):
                raise AssertionError("rollback is not part of this test")

        action = _make_action()
        result = ExecutionAgent(adapters=[LeakyAdapter()]).execute(
            action, _make_safety(action)
        )

        assert "hunter2" not in result.output
        assert result.pre_snapshot == {"db_password": "[REDACTED]", "health": "ok"}
        assert result.post_snapshot == {"client_secret": "[REDACTED]", "health": "ok"}

    def test_live_dispatch_is_reserved_for_safe_executor_workflow(self):
        action = _make_action()
        safety = _make_safety(action)

        with pytest.raises(ExecutionAgentError, match="Safe Executor"):
            self.agent.execute(action, safety, dry_run=False)
