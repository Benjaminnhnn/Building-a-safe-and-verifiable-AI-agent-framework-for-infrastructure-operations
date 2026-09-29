from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.adapters import SafeActionRequest
from core.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment


def _request(**changes: object) -> SafeActionRequest:
    action = TypedAction(
        action_id="act-db-01",
        incident_id="inc-db-01",
        action_type=ActionType.REMOVE_SCOPED_PORT_BLOCK,
        target_resource_id="postgres-db",
        environment=Environment.STAGING,
        reason="evidence-backed staging DB fault",
        evidence_refs=["ev-1", "ev-2", "ev-3"],
        expected_outcome="database connectivity restored",
        reversible=True,
        rollback_plan=RollbackPlan(available=True),
    )
    fields: dict[str, object] = {
        "request_id": "req-1",
        "idempotency_key": "idem-1",
        "catalog_action_id": "remove_scoped_db_reject",
        "scenario_id": "DB-01",
        "target_scope": "staging_moodle_nodes",
        "action": action,
    }
    fields.update(changes)
    return SafeActionRequest(**fields)


def _gate(**changes: object) -> SafeExecutionGate:
    fields: dict[str, object] = {"approval_signing_key": "test-key", "approved_actors": {"alice"}}
    fields.update(changes)
    return SafeExecutionGate(**fields)


def test_gate_allows_reviewed_high_confidence_request_once() -> None:
    gate = _gate()
    request = _request()
    verdict = gate.evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "ALLOW"
    assert verdict.approval_required is False
    assert gate.evaluate(request, actor_role="executor", confidence=0.9).decision == "DENY"


def test_gate_denies_kill_switch_insufficient_evidence_and_production() -> None:
    assert _gate(kill_switch_enabled=True).evaluate(_request(), actor_role="executor", confidence=0.9).decision == "DENY"
    insufficient = _request(action=_request().action.model_copy(update={"evidence_refs": ["ev-1", "ev-2"]}))
    assert _gate().evaluate(insufficient, actor_role="executor", confidence=0.9).decision == "DENY"
    production = _request(action=_request().action.model_copy(update={"environment": Environment.PRODUCTION}))
    assert _gate().evaluate(production, actor_role="executor", confidence=0.9).decision == "DENY"


def test_gate_requires_valid_short_lived_action_bound_approval() -> None:
    request = _request(idempotency_key="idem-approval")
    gate = _gate()
    assert gate.evaluate(request, actor_role="executor", confidence=0.7).decision == "REQUIRE_APPROVAL"
    approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key"
    )
    assert gate.evaluate(request, actor_role="executor", confidence=0.7, approval=approval).decision == "ALLOW"


def test_gate_rejects_wrong_actor_expired_or_tampered_approval() -> None:
    request = _request(idempotency_key="idem-negative")
    gate = _gate()
    approval = ApprovalRecord.create(action_sha256=gate.action_hash(request), actor_id="mallory", signing_key="test-key")
    assert gate.evaluate(request, actor_role="executor", confidence=0.7, approval=approval).decision == "REQUIRE_APPROVAL"
    expired = ApprovalRecord.create(action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key")
    expired.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert gate.evaluate(request, actor_role="executor", confidence=0.7, approval=expired).decision == "REQUIRE_APPROVAL"

