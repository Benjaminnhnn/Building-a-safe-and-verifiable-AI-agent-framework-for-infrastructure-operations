from __future__ import annotations

from core.adapters import SafeLiveActionRequest
from core.safe_action_audit import SafeActionAuditStore
from core.safe_action_live import SafeActionLiveWorkflow
from core.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.safe_executor_client import action_digest
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment


def _request(scenario: str = "DB-01", action_id: str = "remove_scoped_db_reject", scope: str = "staging_moodle_nodes") -> SafeLiveActionRequest:
    action_type = {
        "remove_scoped_db_reject": ActionType.REMOVE_SCOPED_PORT_BLOCK,
        "remove_named_cpu_load_container": ActionType.STOP_FAULT_INJECTOR,
        "recreate_moodle_web_from_reviewed_compose": ActionType.RUN_ANSIBLE_PLAYBOOK,
        "start_reviewed_compose_service": ActionType.START_CONTAINER,
        "restore_fixture_directory_mode": ActionType.RESTORE_MOODLEDATA_PERMISSION,
    }.get(action_id, ActionType.READ_HEALTH)
    return SafeLiveActionRequest(
        request_id=f"live-{scenario}",
        idempotency_key=f"live-idem-{scenario}-000001",
        catalog_action_id=action_id,
        scenario_id=scenario,
        target_scope=scope,
        action=TypedAction(
            action_id=f"action-{scenario}",
            incident_id=f"incident-{scenario}",
            action_type=action_type,
            target_resource_id="moodle-app",
            environment=Environment.STAGING,
            reason="restore the catalogued staging fault",
            evidence_refs=["ev-alert", "ev-synthetic", "ev-config"],
            expected_outcome="staging fault reset",
            reversible=True,
            rollback_plan=RollbackPlan(available=True),
        ),
    )


def _api_approval(request: SafeLiveActionRequest) -> dict[str, str]:
    return {
        "actor_id": "operator",
        "expires_at": "2099-01-01T00:00:00Z",
        "action_sha256": action_digest(
            request.scenario_id, request.catalog_action_id, request.target_scope, request.idempotency_key
        ),
        "signature": "fixture-signature",
    }


def test_live_workflow_calls_executor_only_after_gate_and_audit(tmp_path) -> None:
    calls = []

    def executor(**kwargs):
        calls.append(kwargs)
        return {"status": "executed", "scenario_id": "DB-01", "completed_nodes": ["a", "b"], "mutated": True}

    audit = SafeActionAuditStore(tmp_path / "audit.db")
    request = _request()
    workflow = SafeActionLiveWorkflow(
        gate=SafeExecutionGate(approval_signing_key="test", approved_actors={"operator"}),
        audit=audit,
        executor=executor,
    )
    try:
        report = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={"fault": "present", "database_password": "must-not-enter-audit"},
            gate_approval=None,
            executor_approval=_api_approval(request),
        )
        assert report["status"] == "awaiting_verification"
        assert report["execution_permitted"] is True
        assert report["mutated"] is True
        assert report["resolution_eligible"] is False
        assert len(calls) == 1
        assert calls[0]["scenario_id"] == "DB-01"
        assert audit.verify_chain("incident-DB-01") is True
        events = audit.list_for_incident("incident-DB-01")
        assert [event["event_type"] for event in events] == [
            "live_gate", "live_pre_snapshot", "live_executor_result", "live_verification_pending"
        ]
        assert events[1]["payload"]["database_password"] == "[REDACTED]"
    finally:
        audit.close()


def test_medium_blast_radius_requires_gate_approval_before_executor(tmp_path) -> None:
    calls = []
    audit = SafeActionAuditStore(tmp_path / "audit.db")
    request = _request("NET-01", "recreate_moodle_web_from_reviewed_compose", "staging_moodle_nodes")
    gate = SafeExecutionGate(approval_signing_key="test-key", approved_actors={"operator"})
    def executor(**kwargs):
        calls.append(kwargs)
        return {"status": "disabled", "scenario_id": "NET-01", "completed_nodes": [], "mutated": False}

    workflow = SafeActionLiveWorkflow(gate=gate, audit=audit, executor=executor)
    try:
        denied = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(request),
        )
        assert denied["status"] == "awaiting_approval"
        assert not calls

        gate_approval = ApprovalRecord.create(
            action_sha256=gate.action_hash(request), actor_id="operator", signing_key="test-key"
        )
        allowed = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=gate_approval,
            executor_approval=_api_approval(request),
        )
        assert allowed["status"] == "escalated"  # executor stub returned no success response
        assert len(calls) == 1
    finally:
        audit.close()


def test_live_workflow_rejects_wrong_api_approval_without_calling_executor(tmp_path) -> None:
    calls = []
    audit = SafeActionAuditStore(tmp_path / "audit.db")
    request = _request()
    workflow = SafeActionLiveWorkflow(
        gate=SafeExecutionGate(approval_signing_key="test", approved_actors={"operator"}),
        audit=audit,
        executor=lambda **kwargs: calls.append(kwargs),
    )
    bad_approval = _api_approval(request)
    bad_approval["action_sha256"] = "0" * 64
    try:
        report = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=bad_approval,
        )
        assert report["status"] == "awaiting_approval"
        assert report["mutated"] is False
        assert not calls
    finally:
        audit.close()
