"""Tests for DryRunner, ReadOnlyTargetClient, and ReadOnlyGuardClient.

Verifications:
1. Matching preconditions: returns ALLOW and correct preview with diff and affected resources.
2. Mismatching preconditions: returns DENY with explicit reason and failed preconditions.
3. Target resource missing: returns DENY.
4. Dry-run safety: guarantees no mutating calls are made; mock client records zero mutations.
5. Guard client: actively blocks any mutating or unapproved calls with MutationNotAllowedError.
6. Immutability: DryRunResult, DryRunPreview, and StateDiff are frozen.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from evaluation.agent_replay.domain.action_plan import (
    ActionPlan,
    BlastRadius,
    ExpectedScope,
    ReversibleRollback,
)
from evaluation.agent_replay.domain.dry_run import (
    DictReadOnlyClient,
    DryRunDecision,
    DryRunPreview,
    DryRunResult,
    DryRunner,
    MutationNotAllowedError,
    ReadOnlyGuardClient,
    ReadOnlyTargetClient,
    StateDiff,
    evaluate_precondition,
)
from evaluation.agent_replay.domain.enums import ActionType


# ---------------------------------------------------------------------------
# Test Fixtures & Mock Clients
# ---------------------------------------------------------------------------


class MockMutatingClient:
    """Mock client that exposes read methods AND mutating methods.

    Tracks every method call so tests can prove dry-run causes zero mutations.
    """

    def __init__(self, resources: dict[str, dict[str, Any]] | None = None) -> None:
        self._resources = copy.deepcopy(resources) if resources else {}
        self.read_calls: list[tuple[str, Any]] = []
        self.mutation_calls: list[tuple[str, Any]] = []

    # Read methods
    def get_resource_state(self, resource_id: str) -> dict[str, Any] | None:
        self.read_calls.append(("get_resource_state", resource_id))
        state = self._resources.get(resource_id)
        return copy.deepcopy(state) if state is not None else None

    def read_config(self, resource_id: str) -> dict[str, Any]:
        self.read_calls.append(("read_config", resource_id))
        return {"config_version": "1.0"}

    def check_health(self, resource_id: str) -> bool:
        self.read_calls.append(("check_health", resource_id))
        return True

    # Mutating methods
    def restart_service(self, resource_id: str) -> bool:
        self.mutation_calls.append(("restart_service", resource_id))
        if resource_id in self._resources:
            self._resources[resource_id]["status"] = "running"
        return True

    def execute_command(self, cmd: str) -> str:
        self.mutation_calls.append(("execute_command", cmd))
        return "stdout"

    def write_config(self, path: str, content: str) -> None:
        self.mutation_calls.append(("write_config", (path, content)))

    def delete_resource(self, resource_id: str) -> bool:
        self.mutation_calls.append(("delete_resource", resource_id))
        self._resources.pop(resource_id, None)
        return True

    def stop_container(self, container_id: str) -> bool:
        self.mutation_calls.append(("stop_container", container_id))
        return True


def _make_sample_plan(
    *,
    target: str = "moodle-app-b",
    affected: list[str] | None = None,
    action_type: ActionType = ActionType.RESTART_CONTAINER,
    operation: str = "restart moodle web container",
    preconditions: list[str] | None = None,
    blast_radius: BlastRadius = BlastRadius.LOW,
) -> ActionPlan:
    affected_ids = affected or [target]
    if target not in affected_ids:
        affected_ids.append(target)

    return ActionPlan(
        incident_id="inc-test-001",
        action_type=action_type,
        target_resource_id=target,
        operation=operation,
        preconditions=preconditions if preconditions is not None else ["status == stopped"],
        evidence_ids=["evi-test-001"],
        expected_scope=ExpectedScope(
            affected_resource_ids=affected_ids,
            max_blast_radius=blast_radius,
        ),
        expected_outcome="Container is restarted and running healthy",
        rollback=ReversibleRollback(
            steps=["stop moodle-app-b", "start reviewed previous container"],
            max_rollback_seconds=60,
        ),
        requested_by="planner_agent",
    )


# ---------------------------------------------------------------------------
# Test Suite 1: Matching Preconditions -> ALLOW & Preview
# ---------------------------------------------------------------------------


class TestDryRunnerMatchingPreconditions:
    """Verifies that matching preconditions produce ALLOW and complete preview."""

    def test_single_precondition_match_produces_allow(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-b": {"status": "stopped", "env": "staging", "traffic": False}
        })
        plan = _make_sample_plan(
            target="moodle-app-b",
            preconditions=["status == stopped"],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.ALLOW
        assert "All preconditions satisfied" in result.reason
        assert result.plan_hash == plan.plan_hash
        assert result.target_resource_id == "moodle-app-b"
        assert result.failed_preconditions == []
        assert result.evaluated_preconditions == ["status == stopped"]
        assert result.preview is not None

    def test_preview_contains_diff_and_affected_resources(self) -> None:
        affected = ["moodle-app-b", "alb-target-group-b"]
        client = DictReadOnlyClient({
            "moodle-app-b": {
                "status": "stopped",
                "uptime": 0,
                "version": "4.1",
            }
        })
        plan = _make_sample_plan(
            target="moodle-app-b",
            affected=affected,
            action_type=ActionType.RESTART_CONTAINER,
            preconditions=["status == stopped", "uptime == 0"],
            blast_radius=BlastRadius.MEDIUM,
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.ALLOW
        preview = result.preview
        assert preview is not None

        # Scope & Metadata
        assert preview.target_resource_id == "moodle-app-b"
        assert preview.affected_resource_ids == affected
        assert preview.max_blast_radius == BlastRadius.MEDIUM
        assert preview.action_type == ActionType.RESTART_CONTAINER
        assert preview.operation == plan.operation
        assert preview.expected_outcome == plan.expected_outcome

        # States & Diff
        assert preview.current_state["status"] == "stopped"
        assert preview.projected_state["status"] == "running"
        assert preview.projected_state["restarted"] is True

        diff = preview.diff
        assert isinstance(diff, StateDiff)
        assert "status" in diff.changes
        assert diff.changes["status"]["old"] == "stopped"
        assert diff.changes["status"]["new"] == "running"
        assert diff.changes["status"]["type"] == "modified"

        # Unified text diff contains standard markers
        assert "--- a/moodle-app-b (current)" in diff.unified_diff
        assert "+++ b/moodle-app-b (projected)" in diff.unified_diff
        assert "-  \"status\": \"stopped\"" in diff.unified_diff
        assert "+  \"status\": \"running\"" in diff.unified_diff

    def test_multiple_preconditions_all_match(self) -> None:
        client = DictReadOnlyClient({
            "moodle-db": {
                "state": "degraded",
                "connections": 95,
                "environment": "staging",
                "in_maintenance": True,
            }
        })
        plan = _make_sample_plan(
            target="moodle-db",
            action_type=ActionType.RECONFIGURE_SERVICE,
            preconditions=[
                "state == degraded",
                "connections >= 90",
                "environment is staging",
                "in_maintenance",
            ],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.ALLOW
        assert len(result.evaluated_preconditions) == 4
        assert result.failed_preconditions == []

    def test_custom_state_delta_reflected_in_preview(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-a": {"status": "running", "workers": 4}
        })
        plan = _make_sample_plan(
            target="moodle-app-a",
            action_type=ActionType.RECONFIGURE_SERVICE,
            preconditions=["status == running"],
        )

        runner = DryRunner()
        result = runner.run(plan, client, state_delta={"workers": 8, "scaled": True})

        assert result.decision == DryRunDecision.ALLOW
        assert result.preview is not None
        assert result.preview.projected_state["workers"] == 8
        assert result.preview.projected_state["scaled"] is True
        assert result.preview.diff.changes["workers"]["old"] == 4
        assert result.preview.diff.changes["workers"]["new"] == 8
        assert result.preview.diff.changes["scaled"]["type"] == "added"


# ---------------------------------------------------------------------------
# Test Suite 2: Mismatching Preconditions -> DENY
# ---------------------------------------------------------------------------


class TestDryRunnerMismatchingPreconditions:
    """Verifies that mismatching conditions or missing targets return DENY."""

    def test_status_mismatch_returns_deny(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-b": {"status": "running"}  # expected "stopped"
        })
        plan = _make_sample_plan(
            target="moodle-app-b",
            preconditions=["status == stopped"],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.DENY
        assert result.preview is None
        assert "status == 'stopped'" in result.reason
        assert "observed 'running'" in result.reason
        assert result.failed_preconditions == ["status == stopped"]

    def test_missing_target_resource_returns_deny(self) -> None:
        client = DictReadOnlyClient({"other-resource": {"status": "ok"}})
        plan = _make_sample_plan(target="non-existent-resource")

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.DENY
        assert result.preview is None
        assert "non-existent-resource" in result.reason
        assert result.failed_preconditions == ["target_exists: non-existent-resource"]

    def test_numeric_comparison_mismatch_returns_deny(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-a": {"cpu_percent": 35.0}
        })
        plan = _make_sample_plan(
            target="moodle-app-a",
            preconditions=["cpu_percent > 80.0"],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.DENY
        assert "cpu_percent > 80.0" in result.failed_preconditions[0]
        assert "35.0" in result.reason

    def test_membership_mismatch_returns_deny(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-a": {"status": "healthy"}
        })
        plan = _make_sample_plan(
            target="moodle-app-a",
            preconditions=["status in [stopped, degraded, failed]"],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.DENY
        assert result.failed_preconditions == ["status in [stopped, degraded, failed]"]
        assert "healthy" in result.reason

    def test_missing_state_key_returns_deny(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-a": {"status": "stopped"}
        })
        plan = _make_sample_plan(
            target="moodle-app-a",
            preconditions=["status == stopped", "maintenance_window == true"],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.DENY
        assert "maintenance_window" in result.reason
        assert "maintenance_window == true" in result.failed_preconditions

    def test_negation_condition_mismatch_returns_deny(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-a": {"status": "stopped", "locked": True}
        })
        plan = _make_sample_plan(
            target="moodle-app-a",
            preconditions=["not locked"],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.DENY
        assert "not locked" in result.failed_preconditions
        assert "truthy" in result.reason

    def test_partial_precondition_failure_lists_all_failures(self) -> None:
        client = DictReadOnlyClient({
            "moodle-app-a": {"status": "running", "cpu": 20.0, "locked": True}
        })
        plan = _make_sample_plan(
            target="moodle-app-a",
            preconditions=[
                "status == stopped",   # fails
                "cpu < 50.0",          # passes
                "not locked",          # fails
            ],
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.DENY
        assert len(result.failed_preconditions) == 2
        assert "status == stopped" in result.failed_preconditions
        assert "not locked" in result.failed_preconditions
        assert len(result.evaluated_preconditions) == 3


# ---------------------------------------------------------------------------
# Test Suite 3: Dry-Run Causes ZERO Mutations
# ---------------------------------------------------------------------------


class TestDryRunnerZeroMutationGuarantee:
    """Proves that DryRunner strictly makes NO mutating calls."""

    def test_dry_runner_with_mock_client_makes_zero_mutating_calls(self) -> None:
        mock_client = MockMutatingClient({
            "moodle-app-b": {"status": "stopped", "env": "staging"}
        })
        plan = _make_sample_plan(target="moodle-app-b")

        runner = DryRunner()
        result = runner.run(plan, mock_client)

        assert result.decision == DryRunDecision.ALLOW

        # Strict assertion: no mutating calls were ever invoked on the client
        assert mock_client.mutation_calls == [], (
            f"Mutating calls occurred during dry-run: {mock_client.mutation_calls}"
        )

        # Read calls were made as expected
        assert len(mock_client.read_calls) >= 1
        assert ("get_resource_state", "moodle-app-b") in mock_client.read_calls

    def test_guarded_client_blocks_mutating_restart(self) -> None:
        raw_client = MockMutatingClient({"res-1": {"status": "stopped"}})
        guarded = ReadOnlyGuardClient(raw_client)

        with pytest.raises(MutationNotAllowedError) as exc_info:
            guarded.restart_service("res-1")

        assert "restart_service" in str(exc_info.value)
        assert "strictly forbidden during dry-run" in str(exc_info.value)
        assert raw_client.mutation_calls == []
        assert "restart_service" in guarded.mutation_attempts

    def test_guarded_client_blocks_mutating_execute_command(self) -> None:
        raw_client = MockMutatingClient()
        guarded = ReadOnlyGuardClient(raw_client)

        with pytest.raises(MutationNotAllowedError) as exc_info:
            guarded.execute_command("docker restart moodle-app")

        assert "execute_command" in str(exc_info.value)
        assert raw_client.mutation_calls == []

    def test_guarded_client_blocks_mutating_write_config(self) -> None:
        raw_client = MockMutatingClient()
        guarded = ReadOnlyGuardClient(raw_client)

        with pytest.raises(MutationNotAllowedError):
            guarded.write_config("/etc/moodle/config.php", "<?php ...")

        assert raw_client.mutation_calls == []

    def test_guarded_client_blocks_mutating_delete(self) -> None:
        raw_client = MockMutatingClient({"volume-1": {"size": "10G"}})
        guarded = ReadOnlyGuardClient(raw_client)

        with pytest.raises(MutationNotAllowedError):
            guarded.delete_resource("volume-1")

        assert raw_client.mutation_calls == []

    def test_guarded_client_blocks_mutating_stop(self) -> None:
        raw_client = MockMutatingClient()
        guarded = ReadOnlyGuardClient(raw_client)

        with pytest.raises(MutationNotAllowedError):
            guarded.stop_container("container-1")

        assert raw_client.mutation_calls == []

    def test_guarded_client_allows_approved_read_methods(self) -> None:
        raw_client = MockMutatingClient({"res-1": {"status": "running"}})
        guarded = ReadOnlyGuardClient(raw_client)

        state = guarded.get_resource_state("res-1")
        assert state == {"status": "running"}

        cfg = guarded.read_config("res-1")
        assert cfg == {"config_version": "1.0"}

        health = guarded.check_health("res-1")
        assert health is True

        assert len(guarded.call_history) == 3
        assert guarded.has_mutations() is False
        assert guarded.mutation_attempts == []

    def test_guarded_client_blocks_unrecognized_callable_fail_closed(self) -> None:
        class ClientWithUnknownOp:
            def mysterious_operation(self) -> str:
                return "mutated_or_read?"

        guarded = ReadOnlyGuardClient(ClientWithUnknownOp())

        with pytest.raises(MutationNotAllowedError) as exc_info:
            guarded.mysterious_operation()

        assert "not an approved read-only method" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test Suite 4: ActionType Projections & Evaluator Helpers
# ---------------------------------------------------------------------------


class TestActionTypeProjections:
    """Verifies default state projections across permitted action types."""

    @pytest.mark.parametrize(
        ("action_type", "expected_key", "expected_val"),
        [
            (ActionType.RESTART_CONTAINER, "status", "running"),
            (ActionType.START_CONTAINER, "status", "running"),
            (ActionType.STOP_FAULT_INJECTOR, "status", "stopped"),
            (ActionType.REMOVE_SCOPED_PORT_BLOCK, "port_blocked", False),
            (ActionType.RESTORE_MOODLEDATA_PERMISSION, "permissions", "0775"),
            (ActionType.NETWORK_RECONNECT, "network_connected", True),
            (ActionType.RECONFIGURE_SERVICE, "config_synced", True),
            (ActionType.RUN_ANSIBLE_PLAYBOOK, "playbook_applied", True),
        ],
    )
    def test_projector_handles_standard_actions(
        self, action_type: ActionType, expected_key: str, expected_val: Any
    ) -> None:
        client = DictReadOnlyClient({"target-res": {"status": "degraded"}})
        plan = _make_sample_plan(
            target="target-res",
            action_type=action_type,
            preconditions=[],  # No preconditions to bypass gate
        )

        runner = DryRunner()
        result = runner.run(plan, client)

        assert result.decision == DryRunDecision.ALLOW
        assert result.preview is not None
        assert result.preview.projected_state[expected_key] == expected_val

    def test_evaluate_precondition_helper_cases(self) -> None:
        state = {"port": 8080, "active": True, "env": "staging", "tags": ["web", "moodle"]}

        ok, _ = evaluate_precondition("port == 8080", state)
        assert ok is True

        ok, _ = evaluate_precondition("port != 9090", state)
        assert ok is True

        ok, _ = evaluate_precondition("port > 8000", state)
        assert ok is True

        ok, _ = evaluate_precondition("env: staging", state)
        assert ok is True

        ok, _ = evaluate_precondition("active", state)
        assert ok is True

        ok, _ = evaluate_precondition("target resource exists", state, target_resource_id="x")
        assert ok is True

        ok, _ = evaluate_precondition("environment is staging", state)
        assert ok is True


# ---------------------------------------------------------------------------
# Test Suite 5: Type Safety & Immutability
# ---------------------------------------------------------------------------


class TestTypeSafetyAndImmutability:
    """Verifies that all dry-run domain models are strictly immutable and typed."""

    def test_invalid_plan_type_raises_type_error(self) -> None:
        runner = DryRunner()
        with pytest.raises(TypeError) as exc_info:
            runner.run({"not": "a_plan"}, DictReadOnlyClient())  # type: ignore[arg-type]

        assert "must be an instance of ActionPlan" in str(exc_info.value)

    def test_dry_run_result_is_frozen(self) -> None:
        client = DictReadOnlyClient({"target": {"status": "stopped"}})
        plan = _make_sample_plan(target="target")
        result = DryRunner().run(plan, client)

        with pytest.raises((ValidationError, TypeError)):
            result.decision = DryRunDecision.DENY  # type: ignore[misc]

    def test_dry_run_preview_is_frozen(self) -> None:
        client = DictReadOnlyClient({"target": {"status": "stopped"}})
        plan = _make_sample_plan(target="target")
        result = DryRunner().run(plan, client)
        assert result.preview is not None

        with pytest.raises((ValidationError, TypeError)):
            result.preview.operation = "tampered operation"  # type: ignore[misc]

    def test_state_diff_is_frozen(self) -> None:
        client = DictReadOnlyClient({"target": {"status": "stopped"}})
        plan = _make_sample_plan(target="target")
        result = DryRunner().run(plan, client)
        assert result.preview is not None

        with pytest.raises((ValidationError, TypeError)):
            result.preview.diff.unified_diff = "tampered diff"  # type: ignore[misc]

