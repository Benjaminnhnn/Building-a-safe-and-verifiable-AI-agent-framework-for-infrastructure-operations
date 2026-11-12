from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.safety.action_catalog import ActionCatalog, ActionPermission, CatalogEntry
from core.evidence.adapters import SafeActionRequest
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate
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
            rollback_action_id="reconcile_db01_safe_baseline",
            method="Re-apply the reviewed safe baseline after partial execution.",
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
    fields: dict[str, object] = {
        "approval_signing_key": "test-key",
        "approved_actors": {"alice"},
        "rollback_catalog": ActionCatalog.load_full_catalog(),
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


def test_default_gate_accepts_reviewed_safe_baseline_compensation_pair() -> None:
    request = _request()
    verdict = SafeExecutionGate(
        approval_signing_key="test-key", approved_actors={"alice"}
    ).evaluate(request, actor_role="executor", confidence=0.9)
    assert verdict.decision == "ALLOW"


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


def test_gate_verifies_operator_ed25519_approval_without_private_key() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    request = _request(idempotency_key="idem-ed25519")
    request.action.requires_approval = True
    gate = SafeExecutionGate(
        approval_verification_key=public_key,
        approved_actors={"alice"},
        rollback_catalog=ActionCatalog.load_full_catalog(),
    )
    digest = gate.action_hash(request)
    approval = ApprovalRecord.create_ed25519(
        action_sha256=digest,
        actor_id="alice",
        private_key_pem=private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )

    assert gate.evaluate(request, actor_role="executor", confidence=0.9, approval=approval).decision == "ALLOW"


def test_gate_rejects_ed25519_approval_for_another_action() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    request = _request(idempotency_key="idem-ed25519-wrong-action")
    request.action.requires_approval = True
    gate = SafeExecutionGate(
        approval_verification_key=public_key,
        approved_actors={"alice"},
        rollback_catalog=ActionCatalog.load_full_catalog(),
    )
    approval = ApprovalRecord.create_ed25519(
        action_sha256="0" * 64,
        actor_id="alice",
        private_key_pem=private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )

    assert gate.evaluate(request, actor_role="executor", confidence=0.9, approval=approval).decision == "REQUIRE_APPROVAL"


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


# --- Actionable deny card: diagnose() is advisory and must agree with evaluate() ---

def _deny_cases():
    base = _request()
    return [
        ("kill switch", _gate(kill_switch_enabled=True), _request(), 0.9),
        ("bad confidence", _gate(), _request(), float("nan")),
        ("few evidence", _gate(), _request(action=base.action.model_copy(update={"evidence_refs": ["ev-1"]})), 0.9),
        ("params", _gate(), _request(action=base.action.model_copy(update={"parameters": {"x": "y"}})), 0.9),
        ("unknown action", _gate(), _request(catalog_action_id="flush_all_iptables_rules"), 0.9),
        ("reversibility", _gate(), _request(action=base.action.model_copy(update={"reversible": False})), 0.9),
        ("no rollback plan", _gate(), _request(action=base.action.model_copy(
            update={"rollback_plan": RollbackPlan(available=False)})), 0.9),
        ("type mismatch", _gate(), _request(action=base.action.model_copy(
            update={"action_type": ActionType.START_CONTAINER})), 0.9),
        ("role", _gate(), _request(), 0.9, "unknown-role"),
        ("production", _gate(), _request(action=base.action.model_copy(
            update={"environment": Environment.PRODUCTION})), 0.9),
        ("target", _gate(), _request(action=base.action.model_copy(
            update={"target_resource_id": "prometheus-monitor"})), 0.9),
        ("low confidence", _gate(), _request(), 0.5),
    ]


@pytest.mark.parametrize("case", _deny_cases(), ids=lambda case: case[0])
def test_diagnose_first_failure_matches_evaluate_reason(case) -> None:
    _name, gate, request, confidence, *role = case
    role = role[0] if role else "executor"
    failures = [item for item in gate.diagnose(request, actor_role=role, confidence=confidence)
                if item["status"] == "fail"]
    verdict = gate.evaluate(request, actor_role=role, confidence=confidence)
    assert verdict.decision == "DENY"
    assert failures and failures[0]["reason"] == verdict.reason


def test_diagnose_reports_every_failing_rule_and_classifies_them() -> None:
    request = _request(action=_request().action.model_copy(update={"evidence_refs": ["ev-1", "ev-2"]}))
    summary = _gate().explain(request, actor_role="executor", confidence=0.5)
    assert summary["decision"] == "DENY" and summary["checks"] == []
    rules = {item["rule_id"]: item["class"] for item in summary["failed_rules"]}
    assert rules == {"G04": "REMEDIABLE", "G14": "REMEDIABLE"}


def test_diagnose_marks_catalog_dependent_rules_not_evaluated_for_unknown_action() -> None:
    results = _gate().diagnose(_request(catalog_action_id="drop_database"), actor_role="executor", confidence=0.9)
    by_id = {item["rule_id"]: item for item in results}
    assert by_id["G06"]["status"] == "fail" and by_id["G06"]["class"] == "HARD"
    assert all(by_id[rule]["status"] == "not_evaluated" for rule in ("G07", "G08", "G09", "G10", "G11", "G13"))


def test_diagnose_has_no_side_effects_and_grants_nothing() -> None:
    gate = _gate()
    request = _request()
    for _ in range(3):
        results = gate.diagnose(request, actor_role="executor", confidence=0.9)
        assert all(item["status"] == "pass" for item in results)
        assert all("decision" not in item for item in results)
    # diagnose must not reserve the idempotency key: the real gate still allows once.
    assert gate.evaluate(request, actor_role="executor", confidence=0.9).decision == "ALLOW"
    assert gate.evaluate(request, actor_role="executor", confidence=0.9).decision == "DENY"
    reused = [item for item in gate.diagnose(request, actor_role="executor", confidence=0.9)
              if item["status"] == "fail"]
    assert [item["rule_id"] for item in reused] == ["G03"]


def test_explain_for_allowed_request_has_no_failed_rules_key() -> None:
    summary = _gate().explain(_request(), actor_role="executor", confidence=0.9)
    assert summary["decision"] != "DENY" and "failed_rules" not in summary
