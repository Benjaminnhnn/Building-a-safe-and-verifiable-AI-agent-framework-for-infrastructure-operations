from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.safety.action_catalog import ActionCatalog, ActionPermission, CatalogEntry
from core.evidence.adapters import ActionRequest, SafeActionRequest, SafeLiveActionRequest
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_action_live import SafeActionLiveWorkflow
from core.safety.safe_execution_gate import ApprovalRecord, GateVerdict, SafeExecutionGate
from core.safety.safe_executor_client import action_digest
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment


def _request(scenario: str = "DB-01", action_id: str = "remove_scoped_db_reject", scope: str = "staging_moodle_nodes") -> SafeLiveActionRequest:
    action_type = {
        "remove_scoped_db_reject": ActionType.REMOVE_SCOPED_PORT_BLOCK,
        "reconcile_db01_safe_baseline": ActionType.REMOVE_SCOPED_PORT_BLOCK,
        "remove_named_cpu_load_container": ActionType.STOP_FAULT_INJECTOR,
        "reconcile_res01_safe_baseline": ActionType.STOP_FAULT_INJECTOR,
        "recreate_moodle_web_from_reviewed_compose": ActionType.RUN_ANSIBLE_PLAYBOOK,
        "reconcile_net01_safe_baseline": ActionType.RUN_ANSIBLE_PLAYBOOK,
        "start_reviewed_compose_service": ActionType.START_CONTAINER,
        "reconcile_con01_safe_baseline": ActionType.START_CONTAINER,
        "restore_fixture_directory_mode": ActionType.RESTORE_MOODLEDATA_PERMISSION,
        "reconcile_sec02_safe_baseline": ActionType.RESTORE_MOODLEDATA_PERMISSION,
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
                    "remove_scoped_db_reject": "reconcile_db01_safe_baseline",
                    "remove_named_cpu_load_container": "reconcile_res01_safe_baseline",
                    "recreate_moodle_web_from_reviewed_compose": "reconcile_net01_safe_baseline",
                    "start_reviewed_compose_service": "reconcile_con01_safe_baseline",
                    "restore_fixture_directory_mode": "reconcile_sec02_safe_baseline",
                    "reconcile_db01_safe_baseline": "remove_scoped_db_reject",
                    "reconcile_res01_safe_baseline": "remove_named_cpu_load_container",
                    "reconcile_net01_safe_baseline": "recreate_moodle_web_from_reviewed_compose",
                    "reconcile_con01_safe_baseline": "start_reviewed_compose_service",
                    "reconcile_sec02_safe_baseline": "restore_fixture_directory_mode",
                }.get(action_id),
                method="Restore the prior fixture state.",
            ),
        ),
    )


def _api_approval(request: SafeLiveActionRequest) -> dict[str, str]:
    return {
        "actor_id": "human-approver",
        "expires_at": "2099-01-01T00:00:00Z",
        "action_sha256": action_digest(
            request.scenario_id, request.catalog_action_id, request.target_scope, request.idempotency_key
        ),
        "signature": "fixture-signature",
    }


def _test_gate() -> SafeExecutionGate:
    return SafeExecutionGate(
        approval_signing_key="test",
        approved_actors={"operator"},
        rollback_catalog=ActionCatalog.load_full_catalog(),
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


def test_live_workflow_requires_human_gate_approval_for_medium_risk_action(tmp_path) -> None:
    calls = []
    audit = SafeActionAuditStore(tmp_path / "audit.db")
    request = _request("NET-01", "recreate_moodle_web_from_reviewed_compose", "staging_moodle_nodes")
    gate = SafeExecutionGate(approval_signing_key="test-key", approved_actors={"operator"})
    def executor(**kwargs):
        calls.append(kwargs)
        return {"status": "disabled", "scenario_id": "NET-01", "completed_nodes": [], "mutated": False}

    workflow = SafeActionLiveWorkflow(gate=gate, audit=audit, executor=executor)
    try:
        pending = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(request),
        )
        assert pending["status"] == "awaiting_approval"
        assert pending["execution_permitted"] is False
        assert not calls
    finally:
        audit.close()


def test_live_workflow_requires_independent_gate_and_executor_approvers(tmp_path) -> None:
    calls = []
    request = _request().model_copy(update={
        "action": _request().action.model_copy(update={"requires_approval": True})
    })
    audit = SafeActionAuditStore(tmp_path / "independent-approvals.db")
    gate = SafeExecutionGate(
        approval_signing_key="test-key", approved_actors={"human-approver"}
    )
    workflow = SafeActionLiveWorkflow(
        gate=gate, audit=audit, executor=lambda **kwargs: calls.append(kwargs)
    )
    same_actor_approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request),
        actor_id="human-approver",
        signing_key="test-key",
    )
    try:
        result = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=same_actor_approval,
            executor_approval=_api_approval(request),
        )
        assert result["status"] == "awaiting_approval"
        assert result["execution_permitted"] is False
        assert calls == []
        event = next(
            event for event in audit.list_for_incident(request.action.incident_id)
            if event["event_type"] == "live_approvals_rejected"
        )
        assert "different actors" in event["payload"]["reason"]
    finally:
        audit.close()


@pytest.mark.parametrize(
    ("scenario", "action_id", "scope"),
    [
        ("DB-01", "remove_scoped_db_reject", "staging_moodle_nodes"),
        ("RES-01", "remove_named_cpu_load_container", "moodle-app-b"),
        ("NET-01", "recreate_moodle_web_from_reviewed_compose", "staging_moodle_nodes"),
        ("CON-01", "start_reviewed_compose_service", "moodle-app-b"),
        ("SEC-02", "restore_fixture_directory_mode", "moodledata_synthetic_fixture_directory"),
    ],
)
def test_live_workflow_catalogs_safe_baseline_compensation_for_every_s3_3_mutation(
    tmp_path, scenario: str, action_id: str, scope: str
) -> None:
    calls = []
    audit = SafeActionAuditStore(tmp_path / f"{scenario}-audit.db")
    request = _request(scenario, action_id, scope)
    gate = SafeExecutionGate(approval_signing_key="test-key", approved_actors={"operator"})
    workflow = SafeActionLiveWorkflow(
        gate=gate,
        audit=audit,
        executor=lambda **kwargs: calls.append(kwargs) or {
            "status": "disabled", "scenario_id": scenario, "completed_nodes": [], "mutated": False
        },
    )
    try:
        gate_approval = ApprovalRecord.create(
            action_sha256=gate.action_hash(request), actor_id="operator", signing_key="test-key"
        )
        report = workflow.run(
            request,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=gate_approval,
            executor_approval=_api_approval(request),
        )

        assert report["status"] == "escalated"
        assert report["execution_permitted"] is False
        assert len(calls) == 1
        assert calls[0]["catalog_action_id"] == action_id
        assert audit.verify_chain(f"incident-{scenario}") is True
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


def test_unknown_single_node_failure_triggers_approved_safe_baseline_compensation(tmp_path) -> None:
    calls = []
    original = _request("RES-01", "remove_named_cpu_load_container", "moodle-app-b")
    compensation = _rollback_request(original)

    def executor(**kwargs):
        calls.append(kwargs["catalog_action_id"])
        if kwargs["catalog_action_id"] == original.catalog_action_id:
            return {
                "status": "failed",
                "scenario_id": original.scenario_id,
                "completed_nodes": [],
                "failure": {"node": "b", "status": "timeout"},
                "mutated": None,
                "mutation_state": "unknown",
            }
        return {
            "status": "executed",
            "scenario_id": original.scenario_id,
            "completed_nodes": ["b"],
            "mutated": True,
        }

    audit = SafeActionAuditStore(tmp_path / "unknown-single-node-mutation.db")
    workflow = SafeActionLiveWorkflow(gate=_AllowGate(), audit=audit, executor=executor)
    try:
        first = workflow.run(
            original,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(original),
        )
        compensation_result = workflow.rollback_after_failure(
            original,
            compensation,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(compensation),
        )

        assert first["status"] == "escalated"
        assert first["execution_permitted"] is True
        assert first["mutated"] is None
        assert first["mutation_state"] == "unknown"
        assert compensation_result["status"] == "awaiting_verification"
        assert calls == [original.catalog_action_id, compensation.catalog_action_id]
        events = audit.list_for_incident(original.action.incident_id)
        requested = next(event for event in events if event["event_type"] == "conditional_rollback_requested")
        assert requested["payload"]["trigger"] == "unknown_mutation"
        assert audit.verify_chain(original.action.incident_id)
    finally:
        audit.close()


def test_executor_transport_exception_triggers_approved_safe_baseline_compensation(tmp_path) -> None:
    calls = []
    original = _request("RES-01", "remove_named_cpu_load_container", "moodle-app-b")
    compensation = _rollback_request(original)

    def executor(**kwargs):
        calls.append(kwargs["catalog_action_id"])
        if kwargs["catalog_action_id"] == original.catalog_action_id:
            raise TimeoutError("executor response was lost")
        return {
            "status": "executed",
            "scenario_id": original.scenario_id,
            "completed_nodes": ["b"],
            "mutated": True,
        }

    audit = SafeActionAuditStore(tmp_path / "transport-exception.db")
    workflow = SafeActionLiveWorkflow(gate=_AllowGate(), audit=audit, executor=executor)
    try:
        first = workflow.run(
            original,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(original),
        )
        compensation_result = workflow.rollback_after_failure(
            original,
            compensation,
            actor_role="executor",
            confidence=0.9,
            pre_snapshot={},
            gate_approval=None,
            executor_approval=_api_approval(compensation),
        )

        assert first["status"] == "escalated"
        assert first["mutated"] is None
        assert first["mutation_state"] == "unknown"
        assert first["result"]["status"] == "transport_error"
        assert compensation_result["status"] == "awaiting_verification"
        assert calls == [original.catalog_action_id, compensation.catalog_action_id]
        assert audit.verify_chain(original.action.incident_id)
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
    original_entry = ActionCatalog.load_moodle_s3_3_execution_catalog().lookup(
        original.catalog_action_id
    )
    assert original_entry is not None
    workflow = SafeActionLiveWorkflow(
        gate=_AllowGate(),
        audit=audit,
        catalog=ActionCatalog([original_entry]),
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
