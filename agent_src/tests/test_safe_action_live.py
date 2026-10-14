from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.action_catalog import ActionCatalog, ActionPermission, CatalogEntry
from core.adapters import ActionRequest, SafeActionRequest, SafeLiveActionRequest
from core.safe_action_audit import SafeActionAuditStore
from core.safe_action_live import SafeActionLiveWorkflow
from core.safe_execution_gate import ApprovalRecord, GateVerdict, SafeExecutionGate
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
            target_resource_id=("moodledata-volume" if action_id == "restore_fixture_directory_mode" else "moodle-app"),
            environment=Environment.STAGING,
            reason="restore the catalogued staging fault",
            evidence_refs=["ev-alert", "ev-synthetic", "ev-config"],
            expected_outcome="staging fault reset",
            reversible=True,
            rollback_plan=RollbackPlan(
                available=True,
                rollback_action_id={
                    "remove_scoped_db_reject": "restore_scoped_db_reject",
                    "start_reviewed_compose_service": "stop_reviewed_compose_service",
                }.get(action_id),
                method="Restore the prior fixture state.",
            ),
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


def _test_gate() -> SafeExecutionGate:
    inverse = CatalogEntry(
        action_id="restore_scoped_db_reject",
        description="Test-only inverse entry for gate-flow coverage.",
        adapter="docker",
        permission=ActionPermission(required_role="executor", environment_scope=["staging"]),
        blast_radius="LOW",
        is_reversible=True,
        rollback_action="remove_scoped_db_reject",
        idempotent=True,
    )
    return SafeExecutionGate(
        approval_signing_key="test",
        approved_actors={"operator"},
        rollback_catalog=ActionCatalog([inverse]),
    )


class _AllowGate:
    def evaluate(self, request, **_kwargs):
        return GateVerdict(
            decision="ALLOW",
            reason="test policy allow",
            action_sha256=SafeExecutionGate.action_hash(request),
        )

    def release_before_execution(self, *_args, **_kwargs):
        return True


def _rollback_request(original: SafeLiveActionRequest) -> SafeLiveActionRequest:
    return original.model_copy(
        update={
            "request_id": "rollback-" + original.request_id,
            "idempotency_key": "rollback-" + original.idempotency_key,
            "catalog_action_id": original.action.rollback_plan.rollback_action_id,
            "action": original.action.model_copy(
                update={
                    "action_id": "rollback-" + original.action.action_id,
                    "reason": "restore state after the partial failure",
                    "rollback_plan": RollbackPlan(
                        available=True,
                        rollback_action_id=original.catalog_action_id,
                        method="Reapply the original scoped state.",
                    ),
                }
            ),
        }
    )


def _reciprocal_test_catalog(original: SafeLiveActionRequest) -> ActionCatalog:
    permission = ActionPermission(required_role="executor", environment_scope=["staging"])
    return ActionCatalog(
        [
            CatalogEntry(
                action_id=original.catalog_action_id,
                description="test forward mutation",
                adapter="docker",
                permission=permission,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=original.action.rollback_plan.rollback_action_id,
                idempotent=True,
            ),
            CatalogEntry(
                action_id=original.action.rollback_plan.rollback_action_id,
                description="test reciprocal rollback",
                adapter="docker",
                permission=permission,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=original.catalog_action_id,
                idempotent=True,
            ),
        ]
    )
def test_executor_requests_reject_unknown_fields_and_coerced_flags() -> None:
    live_payload = _request().model_dump()
    live_payload["shell_command"] = "unsafe"
    with pytest.raises(ValidationError):
        SafeLiveActionRequest.model_validate(live_payload)

    dry_payload = _request().model_dump()
    dry_payload["dry_run"] = True
    with pytest.raises(ValidationError):
        SafeLiveActionRequest.model_validate(dry_payload)

    action = _request().action
    with pytest.raises(ValidationError):
        ActionRequest(
            request_id="req-coerced",
            idempotency_key="idem-coerced",
            action=action,
            dry_run="false",
        )

    with pytest.raises(ValidationError):
        SafeActionRequest(**(_request().model_dump() | {"dry_run": False}))


def test_live_workflow_calls_executor_only_after_gate_and_audit(tmp_path) -> None:
    calls = []

    def executor(**kwargs):
        calls.append(kwargs)
        return {"status": "executed", "scenario_id": "DB-01", "completed_nodes": ["a", "b"], "mutated": True}

    audit = SafeActionAuditStore(tmp_path / "audit.db")
    request = _request()
    workflow = SafeActionLiveWorkflow(
        gate=_test_gate(),
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


def test_live_workflow_denies_catalog_action_without_rollback_before_executor(tmp_path) -> None:
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
        assert denied["status"] == "denied"
        assert "catalogued reversible rollback plan" in denied["reason"]
        assert not calls
    finally:
        audit.close()


def test_live_workflow_rejects_wrong_api_approval_without_calling_executor(tmp_path) -> None:
    calls = []
    audit = SafeActionAuditStore(tmp_path / "audit.db")
    request = _request()
    workflow = SafeActionLiveWorkflow(
        gate=_test_gate(),
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


def test_live_workflow_can_retry_same_request_after_approval_rejection(tmp_path) -> None:
    calls = []
    audit = SafeActionAuditStore(tmp_path / "audit.db")
    request = _request()
    workflow = SafeActionLiveWorkflow(
        gate=_test_gate(),
        audit=audit,
        executor=lambda **kwargs: calls.append(kwargs) or {
            "status": "executed", "scenario_id": "DB-01", "completed_nodes": ["a"], "mutated": True
        },
    )
    bad_approval = _api_approval(request) | {"action_sha256": "0" * 64}
    try:
        first = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=bad_approval,
        )
        second = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(request),
        )

        assert first["status"] == "awaiting_approval"
        assert second["status"] == "awaiting_verification"
        assert len(calls) == 1
    finally:
        audit.close()


def test_conditional_rollback_runs_only_for_audited_partial_mutation(tmp_path) -> None:
    calls = []
    original = _request()
    rollback = _rollback_request(original)

    def executor(**kwargs):
        calls.append(kwargs["catalog_action_id"])
        if kwargs["catalog_action_id"] == original.catalog_action_id:
            return {
                "status": "partial_failure",
                "scenario_id": original.scenario_id,
                "completed_nodes": ["a"],
                "failure": {"node": "b", "status": "failed", "exit_code": 1},
                "mutated": True,
            }
        return {
            "status": "executed",
            "scenario_id": original.scenario_id,
            "completed_nodes": ["a"],
            "mutated": True,
        }

    audit = SafeActionAuditStore(tmp_path / "conditional-rollback.db")
    workflow = SafeActionLiveWorkflow(
        gate=_AllowGate(),
        audit=audit,
        catalog=_reciprocal_test_catalog(original),
        executor=executor,
    )
    try:
        first = workflow.run(
            original,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={"state": "faulted"},
            gate_approval=None,
            executor_approval=_api_approval(original),
        )
        rollback_result = workflow.rollback_after_failure(
            original,
            rollback,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={"state": "partially-mutated"},
            gate_approval=None,
            executor_approval=_api_approval(rollback),
        )

        assert first["result"]["status"] == "partial_failure"
        assert rollback_result["status"] == "awaiting_verification"
        assert rollback_result["resolution_eligible"] is False
        assert calls == [original.catalog_action_id, rollback.catalog_action_id]
        assert audit.verify_chain(original.action.incident_id)
        event_types = [event["event_type"] for event in audit.list_for_incident(original.action.incident_id)]
        assert "conditional_rollback_requested" in event_types
        assert event_types[-1] == "conditional_rollback_result"
    finally:
        audit.close()


def test_conditional_rollback_rejects_nonreciprocal_target_without_dispatch(tmp_path) -> None:
    calls = []
    original = _request()
    invalid_rollback = _rollback_request(original).model_copy(
        update={"target_scope": "other_scope"}
    )
    audit = SafeActionAuditStore(tmp_path / "conditional-rollback-deny.db")
    workflow = SafeActionLiveWorkflow(
        gate=_AllowGate(),
        audit=audit,
        catalog=_reciprocal_test_catalog(original),
        executor=lambda **kwargs: calls.append(kwargs) or {
            "status": "partial_failure",
            "scenario_id": original.scenario_id,
            "completed_nodes": ["a"],
            "failure": {"node": "b", "status": "timeout"},
            "mutated": True,
        },
    )
    try:
        workflow.run(
            original,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(original),
        )
        denied = workflow.rollback_after_failure(
            original,
            invalid_rollback,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(invalid_rollback),
        )

        assert denied["status"] == "denied"
        assert "reciprocal" in denied["reason"]
        assert len(calls) == 1
        assert audit.verify_chain(original.action.incident_id)
    finally:
        audit.close()


def test_conditional_rollback_fails_closed_when_runtime_catalog_has_no_inverse(tmp_path) -> None:
    calls = []
    original = _request()
    rollback = _rollback_request(original)
    audit = SafeActionAuditStore(tmp_path / "conditional-rollback-missing-inverse.db")
    workflow = SafeActionLiveWorkflow(
        gate=_test_gate(),
        audit=audit,
        executor=lambda **kwargs: calls.append(kwargs) or {
            "status": "partial_failure",
            "scenario_id": original.scenario_id,
            "completed_nodes": ["a"],
            "failure": {"node": "b", "status": "failed", "exit_code": 1},
            "mutated": True,
        },
    )
    try:
        workflow.run(
            original,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(original),
        )
        denied = workflow.rollback_after_failure(
            original,
            rollback,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(rollback),
        )

        assert denied["status"] == "denied"
        assert "catalog pair is unavailable" in denied["reason"]
        assert len(calls) == 1
        assert audit.verify_chain(original.action.incident_id)
    finally:
        audit.close()


def test_successful_execution_waits_for_verifier_before_rollback_decision(tmp_path) -> None:
    calls = []
    original = _request()
    rollback = _rollback_request(original)
    audit = SafeActionAuditStore(tmp_path / "conditional-rollback-await-verifier.db")
    workflow = SafeActionLiveWorkflow(
        gate=_AllowGate(),
        audit=audit,
        catalog=_reciprocal_test_catalog(original),
        executor=lambda **kwargs: calls.append(kwargs) or {
            "status": "executed",
            "scenario_id": original.scenario_id,
            "completed_nodes": ["a"],
            "mutated": True,
        },
    )
    try:
        workflow.run(
            original,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(original),
        )
        result = workflow.rollback_after_failure(
            original,
            rollback,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(rollback),
        )

        assert result["status"] == "awaiting_verification"
        assert result["execution_permitted"] is False
        assert len(calls) == 1
        assert "conditional_rollback_requested" not in {
            event["event_type"] for event in audit.list_for_incident(original.action.incident_id)
        }
    finally:
        audit.close()


def test_live_workflow_treats_coerced_mutation_flag_as_unknown(tmp_path) -> None:
    request = _request()
    audit = SafeActionAuditStore(tmp_path / "invalid-executor-response.db")
    workflow = SafeActionLiveWorkflow(
        gate=_AllowGate(),
        audit=audit,
        catalog=_reciprocal_test_catalog(request),
        executor=lambda **_: {
            "status": "executed",
            "scenario_id": request.scenario_id,
            "completed_nodes": ["a"],
            "mutated": "false",
            "failure": "password=do-not-report",
        },
    )
    try:
        report = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(request),
        )

        assert report["status"] == "escalated"
        assert report["execution_permitted"] is True
        assert report["mutated"] is None
        assert report["mutation_state"] == "unknown"
        assert report["resolution_eligible"] is False
        event = audit.list_for_incident(request.action.incident_id)[-2]
        assert event["payload"]["status"] == "invalid_response"
        assert "do-not-report" not in str(event["payload"])
    finally:
        audit.close()


def test_live_workflow_rejects_executor_receipt_with_node_both_completed_and_failed(
    tmp_path,
) -> None:
    request = _request()
    audit = SafeActionAuditStore(tmp_path / "contradictory-executor-response.db")
    workflow = SafeActionLiveWorkflow(
        gate=_AllowGate(),
        audit=audit,
        catalog=_reciprocal_test_catalog(request),
        executor=lambda **_: {
            "status": "partial_failure",
            "scenario_id": request.scenario_id,
            "completed_nodes": ["a"],
            "failure": {"node": "a", "status": "failed", "exit_code": 1},
            "mutated": True,
        },
    )
    try:
        report = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(request),
        )

        assert report["status"] == "escalated"
        assert report["execution_permitted"] is True
        assert report["mutated"] is None
        assert report["mutation_state"] == "unknown"
        assert report["resolution_eligible"] is False
        events = audit.list_for_incident(request.action.incident_id)
        assert events[-2]["payload"]["status"] == "invalid_response"
        assert events[-2]["payload"]["error_type"] == "ValueError"
    finally:
        audit.close()
