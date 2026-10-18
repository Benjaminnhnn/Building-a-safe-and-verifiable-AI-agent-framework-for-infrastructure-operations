"""Tests for Phase 2c: Safety Gate, Approval System, GatePass, Audit Log, and Safe Executor.

Mandatory verifications:
1. Executor cannot run without a valid GatePass.
2. Plan modified after approval is rejected / aborted.
3. Expired approval is rejected / aborted.
4. Approval with mismatched plan_hash is rejected / aborted.
5. Approval for target in different scope is rejected / aborted.
6. Every single rejection and abort produces an audit log record in JSONL.

Additional verifications:
- GatePass cannot be instantiated from outside SafetyGate (token checked in constructor).
- HumanGate rejects unauthorized approvers.
- State changes right before execution (preconditions fail or target missing) abort execution.
- Successful end-to-end flow: request approval -> submit decision -> issue GatePass -> execute -> audited.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from evaluation.agent_replay.domain.action_plan import (
    ActionPlan,
    BlastRadius,
    ExpectedScope,
    IrreversibleRollback,
    ReversibleRollback,
    _compute_plan_hash,
)
from evaluation.agent_replay.domain.approval import (
    ApprovalRecord,
    ApprovalScope,
    ApprovalStore,
    HumanGate,
    UnauthorizedApproverError,
)
from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.domain.dry_run import DictReadOnlyClient
from evaluation.agent_replay.domain.enums import ActionType
from evaluation.agent_replay.domain.executor import (
    ExecutionAbortedError,
    GatePassMissingError,
    SafeExecutor,
)
from evaluation.agent_replay.domain.gate_pass import GatePass, _SAFETY_GATE_TOKEN
from evaluation.agent_replay.domain.safety_gate import PolicyDecision, SafetyGate


# ---------------------------------------------------------------------------
# Test Fixtures & Mock Clients
# ---------------------------------------------------------------------------


class MockTargetInfrastructureClient:
    """Mock infrastructure client tracking read and mutating calls."""

    def __init__(self, resources: dict[str, dict[str, Any]] | None = None) -> None:
        self._resources = copy.deepcopy(resources) if resources else {}
        self.mutations_executed: list[str] = []

    def get_resource_state(self, resource_id: str) -> dict[str, Any] | None:
        state = self._resources.get(resource_id)
        return copy.deepcopy(state) if state is not None else None

    def restart_service(self, resource_id: str) -> str:
        self.mutations_executed.append(f"restart:{resource_id}")
        if resource_id in self._resources:
            self._resources[resource_id]["status"] = "running"
        return f"restarted {resource_id}"

    def set_resource_state(self, resource_id: str, state: dict[str, Any]) -> None:
        self._resources[resource_id] = copy.deepcopy(state)

    def remove_resource(self, resource_id: str) -> None:
        self._resources.pop(resource_id, None)


def _make_plan(
    *,
    target: str = "moodle-app-b",
    affected: list[str] | None = None,
    action_type: ActionType = ActionType.RESTART_CONTAINER,
    operation: str = "restart moodle web container",
    blast_radius: BlastRadius = BlastRadius.MEDIUM,
    preconditions: list[str] | None = None,
    reversible: bool = True,
) -> ActionPlan:
    scope = ExpectedScope(
        affected_resource_ids=affected or [target],
        max_blast_radius=blast_radius,
    )
    rollback = (
        ReversibleRollback(steps=["stop container"], max_rollback_seconds=60)
        if reversible
        else IrreversibleRollback(reason="Requires manual snapshot recovery")
    )
    return ActionPlan(
        incident_id="inc-test-01",
        action_type=action_type,
        target_resource_id=target,
        operation=operation,
        preconditions=preconditions if preconditions is not None else ["status == error"],
        evidence_ids=["ev-01", "ev-02", "ev-03"],
        expected_scope=scope,
        expected_outcome="Container restored to running state",
        rollback=rollback,
        requested_by="planner_agent",
    )


# ===========================================================================
# 1. GatePass Construction Security Tests
# ===========================================================================


def test_gate_pass_direct_instantiation_forbidden() -> None:
    """Constructor must reject instantiation from outside SafetyGate."""
    with pytest.raises(PermissionError, match="GatePass can only be constructed internally by SafetyGate"):
        GatePass(
            plan_hash="a" * 64,
            target_resource_id="moodle-app-b",
            action_type=ActionType.RESTART_CONTAINER,
        )


def test_gate_pass_fake_token_rejected() -> None:
    """Constructor must reject invalid / fake token."""
    with pytest.raises(PermissionError, match="GatePass can only be constructed internally by SafetyGate"):
        GatePass(
            plan_hash="a" * 64,
            target_resource_id="moodle-app-b",
            action_type=ActionType.RESTART_CONTAINER,
            _token="fake_token",
        )


def test_gate_pass_immutability() -> None:
    """GatePass attributes cannot be mutated after creation."""
    gate_pass = GatePass(
        plan_hash="a" * 64,
        target_resource_id="moodle-app-b",
        action_type=ActionType.RESTART_CONTAINER,
        _token=_SAFETY_GATE_TOKEN,
    )
    with pytest.raises(AttributeError, match="immutable value object"):
        gate_pass.plan_hash = "b" * 64  # type: ignore[misc]


# ===========================================================================
# 2. Mandatory Test: Executor cannot run without GatePass
# ===========================================================================


def test_executor_refuses_execution_without_gate_pass() -> None:
    """Executor must raise GatePassMissingError if gate_pass is None and log audit."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger)
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    with pytest.raises(GatePassMissingError, match="Executor cannot run without a valid GatePass"):
        executor.execute(plan, gate_pass=None, client=client)  # type: ignore[arg-type]

    # Verify audit log entry was written
    entries = audit_logger.get_entries_for_plan(plan.plan_hash)
    assert len(entries) == 1
    assert entries[0].reason_code == "GATEPASS_MISSING"
    assert entries[0].result == "REJECTED"


def test_executor_refuses_execution_with_invalid_object_as_gate_pass() -> None:
    """Executor must reject dummy or forged objects not satisfying GatePass."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger)
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    with pytest.raises(GatePassMissingError):
        executor.execute(plan, gate_pass="not_a_gate_pass", client=client)  # type: ignore[arg-type]

    assert audit_logger.has_rejection(plan.plan_hash, "GATEPASS_MISSING")


# ===========================================================================
# 3. Mandatory Test: Plan modified after approval is rejected
# ===========================================================================


def test_plan_modified_after_approval_rejected_at_safety_gate() -> None:
    """If plan attributes are tampered after approval creation, SafetyGate rejects it."""
    audit_logger = SafetyAuditLogger()
    gate = SafetyGate(authorized_approvers={"alice"}, audit_logger=audit_logger)
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    # Create approval for the original plan
    now = datetime.now(timezone.utc)
    approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    # Tamper with the plan (bypass frozen model via object.__setattr__ to simulate tampered object)
    object.__setattr__(plan, "operation", "malicious_unapproved_command")

    result = gate.evaluate_and_issue_pass(plan, client, approval=approval)
    assert not result.permitted
    assert result.gate_pass is None
    assert result.reason_code == "PLAN_HASH_TAMPERED"

    # Verify audit entry exists
    assert audit_logger.has_rejection(plan.plan_hash, "PLAN_HASH_TAMPERED")


def test_plan_modified_after_gate_pass_aborted_by_executor() -> None:
    """If plan is modified between GatePass issuance and execution, Executor aborts."""
    audit_logger = SafetyAuditLogger()
    gate = SafetyGate(authorized_approvers={"alice"}, audit_logger=audit_logger)
    executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    # Valid approval
    now = datetime.now(timezone.utc)
    approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    # Pass SafetyGate
    gate_result = gate.evaluate_and_issue_pass(plan, client, approval=approval)
    assert gate_result.permitted
    gate_pass = gate_result.gate_pass
    assert gate_pass is not None

    # Tamper with plan before execution
    object.__setattr__(plan, "operation", "tampered_after_gate_pass")

    with pytest.raises(ExecutionAbortedError, match="plan was modified after approval"):
        executor.execute(plan, gate_pass, client, approval=approval)

    assert audit_logger.has_rejection(plan.plan_hash, "PLAN_HASH_MISMATCH")
    assert len(client.mutations_executed) == 0


# ===========================================================================
# 4. Mandatory Test: Approval expired is rejected
# ===========================================================================


def test_approval_expired_rejected_at_safety_gate() -> None:
    """SafetyGate rejects an expired approval and logs audit."""
    audit_logger = SafetyAuditLogger()
    gate = SafetyGate(authorized_approvers={"alice"}, audit_logger=audit_logger)
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    # Expired approval
    now = datetime.now(timezone.utc)
    approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(seconds=1),
    )

    result = gate.evaluate_and_issue_pass(plan, client, approval=approval)
    assert not result.permitted
    assert result.reason_code == "APPROVAL_EXPIRED"
    assert result.gate_pass is None

    assert audit_logger.has_rejection(plan.plan_hash, "APPROVAL_EXPIRED")


def test_approval_expired_before_execution_aborted_by_executor() -> None:
    """Executor aborts if approval expires right before execution."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    gate_pass = GatePass(
        plan_hash=plan.plan_hash,
        target_resource_id=plan.target_resource_id,
        action_type=plan.action_type,
        approver="alice",
        _token=_SAFETY_GATE_TOKEN,
    )

    now = datetime.now(timezone.utc)
    expired_approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now - timedelta(minutes=15),
        expires_at=now - timedelta(seconds=1),
    )

    with pytest.raises(ExecutionAbortedError, match="approval expired"):
        executor.execute(plan, gate_pass, client, approval=expired_approval)

    assert audit_logger.has_rejection(plan.plan_hash, "APPROVAL_EXPIRED")
    assert len(client.mutations_executed) == 0


# ===========================================================================
# 5. Mandatory Test: Approval with wrong plan_hash is rejected
# ===========================================================================


def test_approval_wrong_plan_hash_rejected_at_safety_gate() -> None:
    """SafetyGate rejects an approval signed for a different plan_hash."""
    audit_logger = SafetyAuditLogger()
    gate = SafetyGate(authorized_approvers={"alice"}, audit_logger=audit_logger)
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    now = datetime.now(timezone.utc)
    wrong_hash_approval = ApprovalRecord(
        plan_hash="f" * 64,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    result = gate.evaluate_and_issue_pass(plan, client, approval=wrong_hash_approval)
    assert not result.permitted
    assert result.reason_code == "PLAN_HASH_MISMATCH"
    assert result.gate_pass is None

    assert audit_logger.has_rejection(plan.plan_hash, "PLAN_HASH_MISMATCH")


def test_approval_wrong_plan_hash_aborted_by_executor() -> None:
    """Executor aborts if approval plan_hash does not match the executed plan."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    gate_pass = GatePass(
        plan_hash=plan.plan_hash,
        target_resource_id=plan.target_resource_id,
        action_type=plan.action_type,
        approver="alice",
        _token=_SAFETY_GATE_TOKEN,
    )

    now = datetime.now(timezone.utc)
    mismatched_approval = ApprovalRecord(
        plan_hash="e" * 64,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    with pytest.raises(ExecutionAbortedError, match="approval plan_hash .* does not match plan"):
        executor.execute(plan, gate_pass, client, approval=mismatched_approval)

    assert audit_logger.has_rejection(plan.plan_hash, "PLAN_HASH_MISMATCH")
    assert len(client.mutations_executed) == 0


# ===========================================================================
# 6. Mandatory Test: Approval for target in different scope is rejected
# ===========================================================================


def test_approval_wrong_target_rejected_at_safety_gate() -> None:
    """Approval for different target resource is rejected by SafetyGate."""
    audit_logger = SafetyAuditLogger()
    gate = SafetyGate(authorized_approvers={"alice"}, audit_logger=audit_logger)
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan(target="moodle-app-b")

    now = datetime.now(timezone.utc)
    wrong_target_approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target="other-server-node"),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    result = gate.evaluate_and_issue_pass(plan, client, approval=wrong_target_approval)
    assert not result.permitted
    assert result.reason_code == "SCOPE_MISMATCH"
    assert result.gate_pass is None

    assert audit_logger.has_rejection(plan.plan_hash, "SCOPE_MISMATCH")


def test_approval_wrong_action_type_scope_aborted_by_executor() -> None:
    """Approval for different action_type in scope is aborted by Executor."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan(action_type=ActionType.RESTART_CONTAINER)

    gate_pass = GatePass(
        plan_hash=plan.plan_hash,
        target_resource_id=plan.target_resource_id,
        action_type=plan.action_type,
        approver="alice",
        _token=_SAFETY_GATE_TOKEN,
    )

    now = datetime.now(timezone.utc)
    wrong_action_scope_approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=ActionType.START_CONTAINER, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    with pytest.raises(ExecutionAbortedError, match="approval scope .* does not match plan"):
        executor.execute(plan, gate_pass, client, approval=wrong_action_scope_approval)

    assert audit_logger.has_rejection(plan.plan_hash, "SCOPE_MISMATCH")
    assert len(client.mutations_executed) == 0


# ===========================================================================
# 7. Mandatory Test: Every rejection writes an audit line
# ===========================================================================


def test_all_rejection_paths_write_audit_log_entries() -> None:
    """Assert that every rejection condition appends a valid JSONL audit line."""
    with TemporaryDirectory() as tmp_dir:
        audit_file = Path(tmp_dir) / "safety_audit.jsonl"
        audit_logger = SafetyAuditLogger(log_path=audit_file)
        gate = SafetyGate(authorized_approvers={"alice"}, audit_logger=audit_logger)
        executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
        client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
        plan = _make_plan()

        # 1. Missing GatePass rejection
        with pytest.raises(GatePassMissingError):
            executor.execute(plan, gate_pass=None, client=client)  # type: ignore[arg-type]

        # 2. Missing approval rejection
        gate.evaluate_and_issue_pass(plan, client, approval=None)

        # 3. Expired approval rejection
        now = datetime.now(timezone.utc)
        expired_approval = ApprovalRecord(
            plan_hash=plan.plan_hash,
            approver="alice",
            scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
            decided_at=now - timedelta(minutes=20),
            expires_at=now - timedelta(seconds=1),
        )
        gate.evaluate_and_issue_pass(plan, client, approval=expired_approval)

        # 4. Scope mismatch rejection
        wrong_scope_approval = ApprovalRecord(
            plan_hash=plan.plan_hash,
            approver="alice",
            scope=ApprovalScope(action_type=ActionType.START_CONTAINER, target="wrong-target"),
            decided_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        gate.evaluate_and_issue_pass(plan, client, approval=wrong_scope_approval)

        # 5. Precondition failure before execution
        client_bad_precond = MockTargetInfrastructureClient({"moodle-app-b": {"status": "healthy"}})
        gate.evaluate_and_issue_pass(plan, client_bad_precond, approval=None)

        # Verify every audit entry written to file
        lines = [line.strip() for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 5

        for line in lines:
            data = json.loads(line)
            # Check all required fields from requirement 5:
            assert "timestamp" in data
            assert "plan_hash" in data
            assert "policy_decision" in data
            assert "reason_code" in data
            assert "diff_summary" in data
            assert "approver" in data
            assert "result" in data
            assert data["result"] in ("REJECTED", "ABORTED")


# ===========================================================================
# 8. HumanGate & Approver Authorization Tests
# ===========================================================================


def test_human_gate_rejects_unauthorized_approver() -> None:
    """HumanGate must reject decisions from unauthorized approvers."""
    human_gate = HumanGate(authorized_approvers={"alice", "bob"})
    plan = _make_plan()
    scope = ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id)

    # Eve is not authorized
    with pytest.raises(UnauthorizedApproverError, match="Approver 'eve' is not in authorized approver list"):
        human_gate.submit_decision(
            plan_hash=plan.plan_hash,
            approver="eve",
            approved=True,
            scope=scope,
        )


def test_human_gate_authorized_approver_creates_append_only_record() -> None:
    """Authorized approver submits approval which is saved append-only."""
    with TemporaryDirectory() as tmp_dir:
        approval_file = Path(tmp_dir) / "approvals.jsonl"
        store = ApprovalStore(log_path=approval_file)
        human_gate = HumanGate(authorized_approvers={"alice"}, store=store)
        plan = _make_plan()
        scope = ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id)

        approval = human_gate.submit_decision(
            plan_hash=plan.plan_hash,
            approver="alice",
            approved=True,
            scope=scope,
            ttl_seconds=300,
        )

        assert approval is not None
        assert approval.approver == "alice"
        assert approval.plan_hash == plan.plan_hash
        assert approval.scope == scope
        assert not approval.is_expired()

        # Check append-only file
        lines = approval_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        stored = json.loads(lines[0])
        assert stored["plan_hash"] == plan.plan_hash


# ===========================================================================
# 9. Pre-execution Re-Check: Target State & Preconditions Tests
# ===========================================================================


def test_executor_aborts_if_target_disappears_before_execution() -> None:
    """Executor must abort if target resource vanishes right before execution."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    gate_pass = GatePass(
        plan_hash=plan.plan_hash,
        target_resource_id=plan.target_resource_id,
        action_type=plan.action_type,
        approver="alice",
        _token=_SAFETY_GATE_TOKEN,
    )

    now = datetime.now(timezone.utc)
    approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    # Delete the target resource from infrastructure
    client.remove_resource(plan.target_resource_id)

    with pytest.raises(ExecutionAbortedError, match="target resource .* no longer exists"):
        executor.execute(plan, gate_pass, client, approval=approval)

    assert audit_logger.has_rejection(plan.plan_hash, "TARGET_NOT_FOUND")
    assert len(client.mutations_executed) == 0


def test_executor_aborts_if_preconditions_no_longer_hold() -> None:
    """Executor must abort if target state changes such that preconditions fail."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan(preconditions=["status == error"])

    gate_pass = GatePass(
        plan_hash=plan.plan_hash,
        target_resource_id=plan.target_resource_id,
        action_type=plan.action_type,
        approver="alice",
        _token=_SAFETY_GATE_TOKEN,
    )

    now = datetime.now(timezone.utc)
    approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    # Target state changed before execution to status == running
    client.set_resource_state(plan.target_resource_id, {"status": "running"})

    with pytest.raises(ExecutionAbortedError, match="precondition failed immediately before execution"):
        executor.execute(plan, gate_pass, client, approval=approval)

    assert audit_logger.has_rejection(plan.plan_hash, "PRECONDITION_FAILED")
    assert len(client.mutations_executed) == 0


def test_executor_aborts_if_actor_role_lacks_execution_privilege() -> None:
    """Executor must abort if actor_role is unauthorized (e.g. observer)."""
    audit_logger = SafetyAuditLogger()
    executor = SafeExecutor(audit_logger=audit_logger, authorized_approvers={"alice"})
    client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
    plan = _make_plan()

    gate_pass = GatePass(
        plan_hash=plan.plan_hash,
        target_resource_id=plan.target_resource_id,
        action_type=plan.action_type,
        approver="alice",
        _token=_SAFETY_GATE_TOKEN,
    )

    now = datetime.now(timezone.utc)
    approval = ApprovalRecord(
        plan_hash=plan.plan_hash,
        approver="alice",
        scope=ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id),
        decided_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    with pytest.raises(ExecutionAbortedError, match="does not have execution permission"):
        executor.execute(plan, gate_pass, client, approval=approval, actor_role="observer")

    assert audit_logger.has_rejection(plan.plan_hash, "UNAUTHORIZED_ROLE")


# ===========================================================================
# 10. End-to-End Success Workflow Test
# ===========================================================================


def test_complete_successful_execution_pipeline() -> None:
    """Full workflow: plan -> human approval -> gate pass -> pre-execution re-check -> executed."""
    with TemporaryDirectory() as tmp_dir:
        audit_file = Path(tmp_dir) / "audit.jsonl"
        approval_file = Path(tmp_dir) / "approvals.jsonl"

        audit_logger = SafetyAuditLogger(log_path=audit_file)
        approval_store = ApprovalStore(log_path=approval_file)
        human_gate = HumanGate(authorized_approvers={"alice"}, store=approval_store)
        gate = SafetyGate(
            authorized_approvers={"alice"},
            audit_logger=audit_logger,
            approval_store=approval_store,
        )
        executor = SafeExecutor(
            audit_logger=audit_logger,
            approval_store=approval_store,
            authorized_approvers={"alice"},
        )

        client = MockTargetInfrastructureClient({"moodle-app-b": {"status": "error"}})
        plan = _make_plan()

        # Step 1: HumanGate requests approval
        req = human_gate.request_approval(plan, diff_summary="status: error -> running")
        assert req["status"] == "pending_approval"

        # Step 2: Authorized approver approves
        scope = ApprovalScope(action_type=plan.action_type, target=plan.target_resource_id)
        approval = human_gate.submit_decision(
            plan_hash=plan.plan_hash,
            approver="alice",
            approved=True,
            scope=scope,
            ttl_seconds=600,
        )
        assert approval is not None

        # Step 3: SafetyGate verifies all 3 steps and issues GatePass
        gate_result = gate.evaluate_and_issue_pass(plan, client, approval=approval)
        assert gate_result.permitted
        assert gate_result.policy_decision == PolicyDecision.REQUIRE_APPROVAL
        gate_pass = gate_result.gate_pass
        assert isinstance(gate_pass, GatePass)
        assert gate_pass.is_valid_for_plan(plan)

        # Step 4: SafeExecutor runs JIT re-checks and performs mutation
        exec_result = executor.execute(plan, gate_pass, client, approval=approval)
        assert exec_result.success
        assert exec_result.executed
        assert "restart:moodle-app-b" in client.mutations_executed

        # Step 5: Verify persisted JSONL audit entries
        lines = [l.strip() for l in audit_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2  # 1 from gate (PERMITTED), 1 from executor (EXECUTED)

        gate_audit = json.loads(lines[0])
        assert gate_audit["result"] == "PERMITTED"
        assert gate_audit["reason_code"] == "GATE_PASS_ISSUED"
        assert gate_audit["approver"] == "alice"

        exec_audit = json.loads(lines[1])
        assert exec_audit["result"] == "EXECUTED"
        assert exec_audit["reason_code"] == "EXECUTED"
        assert exec_audit["approver"] == "alice"

