from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.action_catalog import ActionCatalog, ActionPermission, CatalogEntry
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
        rollback_plan=RollbackPlan(
            available=True,
            rollback_action_id="restore_scoped_db_reject",
            method="Restore the original scoped firewall rule.",
        ),
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
    rollback_catalog = ActionCatalog([
        CatalogEntry(
            action_id="restore_scoped_db_reject",
            description="Test fixture inverse for the DB-01 gate path.",
            adapter="docker",
            permission=ActionPermission(required_role="executor", environment_scope=["staging"]),
            blast_radius="LOW",
            is_reversible=True,
            rollback_action="remove_scoped_db_reject",
            idempotent=True,
        )
    ])
    fields: dict[str, object] = {
        "approval_signing_key": "test-key",
        "approved_actors": {"alice"},
        "rollback_catalog": rollback_catalog,
    }
    fields.update(changes)
    return SafeExecutionGate(**fields)


def test_catalog_rejects_duplicate_action_ids_instead_of_overwriting_policy() -> None:
    entry = CatalogEntry(
        action_id="probe-db",
        description="Reviewed read-only probe.",
        adapter="probe",
        permission=ActionPermission(required_role="verifier", environment_scope=["staging"], read_only=True),
        blast_radius="LOW",
        is_reversible=True,
        rollback_action=None,
        idempotent=True,
    )

    with pytest.raises(ValueError, match="duplicate action_id in catalog: probe-db"):
        ActionCatalog([entry, entry.model_copy(update={"permission": ActionPermission(
            required_role="executor", environment_scope=["staging"]
        )})])


def test_default_gate_denies_live_action_when_catalog_inverse_is_missing() -> None:
    request = _request()
    verdict = SafeExecutionGate(
        approval_signing_key="test-key", approved_actors={"alice"}
    ).evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "rollback action is missing" in verdict.reason


def test_gate_denies_nonreciprocal_rollback_catalog_entry() -> None:
    unpaired_inverse = CatalogEntry(
        action_id="restore_scoped_db_reject",
        description="Unpaired test inverse.",
        adapter="docker",
        permission=ActionPermission(required_role="executor", environment_scope=["staging"]),
        blast_radius="LOW",
        is_reversible=True,
        rollback_action=None,
        idempotent=True,
    )
    gate = _gate(rollback_catalog=ActionCatalog([unpaired_inverse]))
    verdict = gate.evaluate(_request(), actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "reciprocal staging mutation" in verdict.reason


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


def test_gate_revalidates_nested_action_and_request_copies() -> None:
    malformed_action = _request().action.model_copy(update={"reversible": 1})
    malformed = _request(action=malformed_action)
    verdict = _gate().evaluate(malformed, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "schema validation" in verdict.reason

    malformed_request = _request().model_copy(update={"timeout_seconds": True})
    verdict = _gate().evaluate(malformed_request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "schema validation" in verdict.reason


def test_gate_denies_mutating_action_without_catalogued_rollback_readiness() -> None:
    request = _request()
    request.action.rollback_plan = RollbackPlan(available=False)
    verdict = _gate().evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "rollback plan" in verdict.reason

    request = _request()
    request.action.reversible = False
    verdict = _gate().evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "reversibility" in verdict.reason

    net_action = _request().action.model_copy(update={"action_type": ActionType.RUN_ANSIBLE_PLAYBOOK})
    unreviewed_rollback = _request(
        idempotency_key="idem-missing-catalog-rollback",
        catalog_action_id="recreate_moodle_web_from_reviewed_compose",
        scenario_id="NET-01",
        action=net_action,
    )
    verdict = _gate().evaluate(unreviewed_rollback, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "catalogued reversible rollback plan" in verdict.reason

    wrong_rollback = _request(
        idempotency_key="idem-wrong-rollback-id",
        action=_request().action.model_copy(update={
            "rollback_plan": RollbackPlan(
                available=True,
                rollback_action_id="stop_reviewed_compose_service",
                method="Stop the service.",
            )
        }),
    )
    verdict = _gate().evaluate(wrong_rollback, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "catalogued reversible rollback plan" in verdict.reason


def test_gate_denies_non_finite_or_out_of_range_confidence() -> None:
    for confidence in (float("nan"), float("inf"), -0.1, 1.1, True):
        assert _gate().evaluate(_request(), actor_role="executor", confidence=confidence).decision == "DENY"


def test_gate_denies_unknown_role_and_typed_action_catalog_mismatch() -> None:
    request = _request()
    assert _gate().evaluate(request, actor_role="unknown-role", confidence=0.9).decision == "DENY"
    mismatched_action = request.action.model_copy(
        update={"action_type": ActionType.START_CONTAINER}
    )
    mismatched = _request(action=mismatched_action)
    verdict = _gate().evaluate(mismatched, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "does not match" in verdict.reason


def test_gate_denies_target_resource_outside_reviewed_scope() -> None:
    request = _request(
        action=_request().action.model_copy(update={"target_resource_id": "prometheus-monitor"}),
    )
    verdict = _gate().evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "target resource" in verdict.reason


def test_gate_rejects_ignored_caller_parameters() -> None:
    request = _request(
        action=_request().action.model_copy(update={"parameters": {"service": "unreviewed"}}),
    )
    verdict = _gate().evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "DENY"
    assert "caller parameters" in verdict.reason


def test_gate_requires_valid_short_lived_action_bound_approval() -> None:
    request = _request(idempotency_key="idem-approval")
    gate = _gate()
    assert gate.evaluate(request, actor_role="executor", confidence=0.7).decision == "REQUIRE_APPROVAL"
    approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key"
    )
    assert gate.evaluate(request, actor_role="executor", confidence=0.7, approval=approval).decision == "ALLOW"


def test_planner_approval_requirement_is_enforced_and_hash_bound() -> None:
    request = _request(idempotency_key="idem-planner-approval")
    gate = _gate()
    unsigned_hash = gate.action_hash(request)
    request.action.requires_approval = True

    verdict = gate.evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "REQUIRE_APPROVAL"
    assert verdict.approval_required is True
    assert verdict.action_sha256 != unsigned_hash

    approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key"
    )
    assert gate.evaluate(
        request, actor_role="executor", confidence=0.9, approval=approval
    ).decision == "ALLOW"


def test_approval_is_bound_to_rollback_plan_and_reversibility() -> None:
    request = _request(idempotency_key="idem-rollback-binding")
    gate = _gate()
    approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key"
    )
    request.action.rollback_plan.method = "Different rollback operation."

    verdict = gate.evaluate(request, actor_role="executor", confidence=0.7, approval=approval)
    assert verdict.decision == "REQUIRE_APPROVAL"
    assert verdict.action_sha256 != approval.action_sha256


def test_approval_is_bound_to_action_identity_and_intent() -> None:
    request = _request(idempotency_key="idem-intent-binding")
    gate = _gate()
    approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key"
    )
    request.action.expected_outcome = "Different operator-visible outcome."

    verdict = gate.evaluate(request, actor_role="executor", confidence=0.7, approval=approval)
    assert verdict.decision == "REQUIRE_APPROVAL"
    assert verdict.action_sha256 != approval.action_sha256


def test_gate_rejects_wrong_actor_expired_or_tampered_approval() -> None:
    request = _request(idempotency_key="idem-negative")
    gate = _gate()
    approval = ApprovalRecord.create(action_sha256=gate.action_hash(request), actor_id="mallory", signing_key="test-key")
    assert gate.evaluate(request, actor_role="executor", confidence=0.7, approval=approval).decision == "REQUIRE_APPROVAL"
    expired = ApprovalRecord.create(action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key")
    expired.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert gate.evaluate(request, actor_role="executor", confidence=0.7, approval=expired).decision == "REQUIRE_APPROVAL"
    tampered = ApprovalRecord.create(
        action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key"
    )
    tampered.signature = "0" * 64
    assert gate.evaluate(request, actor_role="executor", confidence=0.7, approval=tampered).decision == "REQUIRE_APPROVAL"


def test_gate_rejects_naive_expiry_timestamp_without_exception() -> None:
    request = _request(idempotency_key="idem-naive-expiry")
    gate = _gate()
    approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request), actor_id="alice", signing_key="test-key"
    )
    approval.expires_at = approval.expires_at.replace(tzinfo=None)
    verdict = gate.evaluate(request, actor_role="executor", confidence=0.7, approval=approval)
    assert verdict.decision == "REQUIRE_APPROVAL"


@pytest.mark.parametrize("ttl_seconds", [0, -1, 901, 1.5, True])
def test_approval_factory_rejects_invalid_ttl(ttl_seconds: int) -> None:
    with pytest.raises(ValueError, match="TTL"):
        ApprovalRecord.create(
            action_sha256="a" * 64,
            actor_id="alice",
            signing_key="test-key",
            ttl_seconds=ttl_seconds,
        )
