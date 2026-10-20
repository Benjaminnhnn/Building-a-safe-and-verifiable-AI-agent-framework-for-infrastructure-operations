"""End-to-end lifecycle integration tests for the safety-gated incident pipeline.

Verifies the complete lifecycle across three critical branches:
1. Controlled happy path (CON-01):
   Alert -> process_moodle_alert(mode='investigate') -> prepare_moodle_live_handoff
   -> dual Ed25519 approvals -> execute_moodle_live_handoff (awaiting_verification)
   -> signed Independent Verifier attestation -> accept_verifier_attestation
   -> RESOLVED with intact SHA-256 audit chain.
2. Partial multi-node failure with automatic rollback (DB-01):
   Execution succeeds on node 'a' and fails on node 'b' -> rollback_after_failure
   re-applies reciprocal safe baseline compensation -> audit chain intact.
3. False recovery rejection / RQ2 protection (CON-01):
   Execution succeeds, but Independent Verifier detects failed forbidden/related probe
   or incomplete stability window (< 120s) -> attestation rejected, incident remains
   in 'verifying' and never transitions to 'resolved'.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.evidence.adapters import SafeLiveActionRequest
from core.evidence.evidence_collectors import EvidenceDraft
from core.evidence.evidence_store import SQLiteEvidenceStore
from core.moodle.alert_integration import process_moodle_alert
from core.moodle.live_handoff import (
    execute_moodle_live_handoff,
    prepare_moodle_live_handoff,
)
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_action_live import SafeActionLiveWorkflow
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.safety.safe_executor_client import action_digest
from core.safety.verifier_attestation import (
    VerifierAttestation,
    VerifierAttestationPayload,
    accept_verifier_attestation,
    canonical_payload,
    get_runtime_verification_status,
)
from core.schema.action import RollbackPlan
from core.schema.verification import (
    ProbeResult,
    StabilityObservation,
    VerificationResult,
)
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS


def _generate_ed25519_pair() -> tuple[Ed25519PrivateKey, bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, private_pem, public_pem


def _con01_alert() -> dict[str, Any]:
    return {
        "status": "firing",
        "fingerprint": "fp-e2e-con01",
        "startsAt": "2026-04-18T10:00:00Z",
        "labels": {
            "alertname": "MoodleWebContainerMissing",
            "service": "moodle",
            "environment": "staging",
        },
        "annotations": {
            "summary": "Moodle web container stopped on node moodle-app-b",
        },
    }


def _db01_alert() -> dict[str, Any]:
    return {
        "status": "firing",
        "fingerprint": "fp-e2e-db01",
        "startsAt": "2026-04-18T10:05:00Z",
        "labels": {
            "alertname": "MoodleNodeRdsTcpFailed",
            "service": "moodle",
            "environment": "staging",
        },
        "annotations": {
            "summary": "Moodle node cannot reach RDS PostgreSQL over TCP 5432",
        },
    }


def _mock_con01_prometheus(monkeypatch: Any) -> None:
    samples = {
        "moodle_fault_con01_web_stopped": [(0.0, "moodle-app-a"), (1.0, "moodle-app-b")],
        "moodle_node_web_running": [(1.0, "moodle-app-a"), (0.0, "moodle-app-b")],
        "moodle_node_up": [(1.0, "moodle-app-a"), (1.0, "moodle-app-b")],
        "moodle_cadvisor_up": [(1.0, "moodle-app-a"), (1.0, "moodle-app-b")],
    }

    def collect(_self: Any, query_id: str, _expression: str, resource_id: str) -> list[EvidenceDraft]:
        if query_id not in samples:
            return []
        return [
            EvidenceDraft(
                "prometheus",
                resource_id,
                "metric_sample",
                datetime.now(timezone.utc),
                {"query_id": query_id, "sample_value": value, "labels": {"instance": instance}},
            )
            for value, instance in samples[query_id]
        ]

    monkeypatch.setattr(
        "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query",
        collect,
    )


def _mock_db01_prometheus(monkeypatch: Any) -> None:
    samples = {
        "moodle_node_rds_tcp_success": [(0.0, "moodle-app-a"), (0.0, "moodle-app-b")],
        "moodle_fault_db01_tagged_rule": [(1.0, "moodle-app-a"), (1.0, "moodle-app-b")],
    }

    def collect(_self: Any, query_id: str, _expression: str, resource_id: str) -> list[EvidenceDraft]:
        if query_id not in samples:
            return []
        return [
            EvidenceDraft(
                "prometheus",
                resource_id,
                "metric_sample",
                datetime.now(timezone.utc),
                {"query_id": query_id, "sample_value": value, "labels": {"instance": instance}},
            )
            for value, instance in samples[query_id]
        ]

    monkeypatch.setattr(
        "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query",
        collect,
    )


def _sign_executor_approval(
    executor_action_sha256: str,
    private_key: Ed25519PrivateKey,
    *,
    actor_id: str = "operator",
) -> dict[str, str]:
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(minutes=5))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    signed = f"{executor_action_sha256}:{actor_id}:{expires_at}".encode("utf-8")
    return {
        "actor_id": actor_id,
        "expires_at": expires_at,
        "action_sha256": executor_action_sha256,
        "signature": base64.b64encode(private_key.sign(signed)).decode("ascii"),
    }


def _build_signed_attestation(
    *,
    incident_id: str,
    request_id: str,
    scenario_id: str,
    receipt_event_hash: str,
    verifier_private: Ed25519PrivateKey,
    sample_count: int = 5,
    sample_interval_seconds: int = 30,
    override_check: tuple[str, bool] | None = None,
) -> VerifierAttestation:
    now = datetime.now(timezone.utc)
    contract = MOODLE_LIVE_CONTRACTS[scenario_id]
    all_checks = set().union(*(set(contract[group]) for group in ("allowed", "forbidden", "related")))
    total_window = sample_interval_seconds * (sample_count - 1)
    samples = []
    for index in range(sample_count):
        checks = {name: True for name in all_checks}
        if override_check is not None:
            checks[override_check[0]] = override_check[1]
        samples.append(
            {
                "observed_at": (
                    now - timedelta(seconds=total_window - sample_interval_seconds * index)
                ).isoformat(),
                "checks": checks,
            }
        )
    observations = [
        StabilityObservation(
            observed_at=item["observed_at"],
            healthy=all(item["checks"][name] for name in contract["allowed"]),
            simulated=False,
        )
        for item in samples
    ]
    verification = VerificationResult(
        verification_id=f"verify-{request_id[:8]}",
        incident_id=incident_id,
        health_passed=all(obs.healthy for obs in observations),
        communication_contract_passed=True,
        stability_seconds=max(120, total_window),
        stability_observations=observations,
        resolution_eligible=True,
        allowed_probes=[
            ProbeResult(
                name=name,
                passed=all(s["checks"][name] for s in samples),
                simulated=False,
                details="live allowed check",
            )
            for name in contract["allowed"]
        ],
        forbidden_probes=[
            ProbeResult(
                name=name,
                passed=all(s["checks"][name] for s in samples),
                simulated=False,
                details="live forbidden check",
            )
            for name in contract["forbidden"]
        ],
        related_probes=[
            ProbeResult(
                name=name,
                passed=all(s["checks"][name] for s in samples),
                simulated=False,
                details="live related check",
            )
            for name in contract["related"]
        ],
        verdict="resolved",
        simulated=False,
        checked_at=now,
    )
    payload = VerifierAttestationPayload.model_validate(
        {
            "version": 1,
            "incident_id": incident_id,
            "request_id": request_id,
            "scenario_id": scenario_id,
            "receipt_event_hash": receipt_event_hash,
            "signed_at": now.isoformat(),
            "verification": verification.model_dump(mode="json"),
            "probe_samples": samples,
        }
    )
    signature = base64.b64encode(verifier_private.sign(canonical_payload(payload))).decode("ascii")
    return VerifierAttestation(payload=payload, signature=signature)


def test_e2e_happy_path_alert_to_verified_resolved_with_intact_audit_chain(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Branch 1: Full controlled happy path from Alertmanager payload to RESOLVED."""
    evidence_db = tmp_path / "evidence.sqlite3"
    audit_db = tmp_path / "safe-action-audit.sqlite3"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(evidence_db))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.delenv("VECTOR_DB_PATH", raising=False)
    _mock_con01_prometheus(monkeypatch)

    # 1. Alert -> Evidence -> Diagnosis -> Remediation Candidate (no execution)
    report = process_moodle_alert(_con01_alert())
    assert report["status"] == "remediation_candidate"
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False
    incident_id = report["incident_id"]
    assert report["remediation_candidate"]["scenario_id"] == "CON-01"
    assert report["remediation_candidate"]["catalog_action_id"] == "start_reviewed_compose_service"

    with SQLiteEvidenceStore(evidence_db) as store:
        assert store.verify_integrity() == len(store.list_for_incident(incident_id))
        decisions = store.query(incident_id=incident_id, source="ai_decision")
        candidate_id = next(
            item.evidence_id
            for item in decisions
            if item.metadata["decision_kind"] == "remediation_candidate"
        )

    # 2. Prepare live handoff -> require dual approvals
    gate_priv, gate_priv_pem, gate_pub_pem = _generate_ed25519_pair()
    prepared = prepare_moodle_live_handoff(
        evidence_db=evidence_db,
        candidate_id=candidate_id,
        gate_public_key=gate_pub_pem,
        gate_actors={"safety-officer"},
    )
    assert prepared["status"] == "approval_required"
    assert prepared["execution_permitted"] is False
    assert prepared["resolution_eligible"] is False
    request_id = prepared["request"]["request_id"]

    # 3. Dual independent Ed25519 approvals + execute_moodle_live_handoff
    gate_approval = ApprovalRecord.create_ed25519(
        action_sha256=prepared["gate_action_sha256"],
        actor_id="safety-officer",
        private_key_pem=gate_priv_pem,
    )
    exec_priv, _, _ = _generate_ed25519_pair()
    executor_approval = _sign_executor_approval(prepared["executor_action_sha256"], exec_priv)

    exec_result = execute_moodle_live_handoff(
        evidence_db=evidence_db,
        candidate_id=candidate_id,
        request_data=prepared["request"],
        gate_approval=gate_approval,
        executor_approval=executor_approval,
        gate_public_key=gate_pub_pem,
        gate_actors={"safety-officer"},
        audit_db=audit_db,
        kill_switch_enabled=False,
        executor=lambda **kwargs: {
            "status": "executed",
            "scenario_id": kwargs["scenario_id"],
            "completed_nodes": ["b"],
            "mutated": True,
        },
    )
    assert exec_result["status"] == "awaiting_verification"
    assert exec_result["execution_permitted"] is True
    assert exec_result["mutated"] is True
    assert exec_result["resolution_eligible"] is False

    # Verify runtime status is 'verifying', not 'resolved'
    status_before = get_runtime_verification_status(
        audit_db=audit_db, incident_id=incident_id, request_id=request_id
    )
    assert status_before["status"] == "verifying"

    with SafeActionAuditStore(audit_db) as audit:
        assert audit.verify_chain(incident_id) is True
        receipt_event = next(
            ev
            for ev in audit.list_for_incident(incident_id)
            if ev["event_type"] == "live_executor_result"
        )
        receipt_hash = receipt_event["event_hash"]

    # 4. Independent Verifier signed attestation -> RESOLVED
    verifier_priv, _, verifier_pub_pem = _generate_ed25519_pair()
    verifier_pub_path = tmp_path / "verifier-public.pem"
    verifier_pub_path.write_bytes(verifier_pub_pem)

    attestation = _build_signed_attestation(
        incident_id=incident_id,
        request_id=request_id,
        scenario_id="CON-01",
        receipt_event_hash=receipt_hash,
        verifier_private=verifier_priv,
    )
    verified = accept_verifier_attestation(
        audit_db=audit_db,
        public_key_path=verifier_pub_path,
        request_id=request_id,
        attestation=attestation,
    )
    assert verified["status"] == "resolved"
    assert verified["idempotent"] is False

    status_after = get_runtime_verification_status(
        audit_db=audit_db, incident_id=incident_id, request_id=request_id
    )
    assert status_after["status"] == "resolved"

    with SafeActionAuditStore(audit_db) as audit:
        assert audit.verify_chain(incident_id) is True
        event_types = [ev["event_type"] for ev in audit.list_for_incident(incident_id)]
        assert event_types == [
            "live_gate",
            "live_pre_snapshot",
            "live_executor_result",
            "live_verification_pending",
            "live_verification_result",
        ]


def test_e2e_partial_failure_triggers_reciprocal_safe_baseline_rollback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Branch 2: Multi-node partial failure triggers audited reciprocal rollback."""
    evidence_db = tmp_path / "evidence-db01.sqlite3"
    audit_db = tmp_path / "audit-db01.sqlite3"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(evidence_db))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.delenv("VECTOR_DB_PATH", raising=False)
    _mock_db01_prometheus(monkeypatch)

    report = process_moodle_alert(_db01_alert())
    assert report["status"] == "remediation_candidate"
    incident_id = report["incident_id"]
    assert report["remediation_candidate"]["scenario_id"] == "DB-01"
    assert report["remediation_candidate"]["catalog_action_id"] == "remove_scoped_db_reject"

    with SQLiteEvidenceStore(evidence_db) as store:
        decisions = store.query(incident_id=incident_id, source="ai_decision")
        candidate_id = next(
            item.evidence_id
            for item in decisions
            if item.metadata["decision_kind"] == "remediation_candidate"
        )

    gate_priv, gate_priv_pem, gate_pub_pem = _generate_ed25519_pair()
    prepared = prepare_moodle_live_handoff(
        evidence_db=evidence_db,
        candidate_id=candidate_id,
        gate_public_key=gate_pub_pem,
        gate_actors={"safety-officer"},
    )
    gate_approval = ApprovalRecord.create_ed25519(
        action_sha256=prepared["gate_action_sha256"],
        actor_id="safety-officer",
        private_key_pem=gate_priv_pem,
    )
    exec_priv, _, _ = _generate_ed25519_pair()
    executor_approval = _sign_executor_approval(prepared["executor_action_sha256"], exec_priv)

    executed_actions: list[str] = []

    def partial_then_rollback_executor(**kwargs: Any) -> dict[str, Any]:
        executed_actions.append(kwargs["catalog_action_id"])
        if kwargs["catalog_action_id"] == "remove_scoped_db_reject":
            return {
                "status": "partial_failure",
                "scenario_id": "DB-01",
                "completed_nodes": ["a"],
                "failure": {"node": "b", "status": "failed", "exit_code": 1},
                "mutated": True,
            }
        return {
            "status": "executed",
            "scenario_id": "DB-01",
            "completed_nodes": ["a", "b"],
            "mutated": True,
        }

    partial_result = execute_moodle_live_handoff(
        evidence_db=evidence_db,
        candidate_id=candidate_id,
        request_data=prepared["request"],
        gate_approval=gate_approval,
        executor_approval=executor_approval,
        gate_public_key=gate_pub_pem,
        gate_actors={"safety-officer"},
        audit_db=audit_db,
        kill_switch_enabled=False,
        executor=partial_then_rollback_executor,
    )
    assert partial_result["status"] == "escalated"
    assert partial_result["mutated"] is True
    assert partial_result["resolution_eligible"] is False

    # Build reciprocal rollback request from the original request
    original_request = SafeLiveActionRequest.model_validate(prepared["request"])
    rollback_action_id = original_request.action.rollback_plan.rollback_action_id
    assert rollback_action_id == "reconcile_db01_safe_baseline"
    rollback_idempotency = f"rollback-{original_request.idempotency_key}"
    rollback_request = original_request.model_copy(
        update={
            "request_id": f"rollback-{original_request.request_id}",
            "idempotency_key": rollback_idempotency,
            "catalog_action_id": rollback_action_id,
            "action": original_request.action.model_copy(
                update={
                    "action_id": f"rollback-{original_request.action.action_id}",
                    "idempotency_key": rollback_idempotency,
                    "reason": "Re-apply safe DB-01 baseline after partial node failure",
                    "rollback_plan": RollbackPlan(
                        available=True,
                        rollback_action_id=original_request.catalog_action_id,
                        method="Reciprocal DB-01 baseline compensation",
                    ),
                }
            ),
        }
    )

    gate = SafeExecutionGate(
        approval_verification_key=gate_pub_pem,
        approved_actors={"safety-officer"},
    )
    rollback_gate_approval = ApprovalRecord.create_ed25519(
        action_sha256=gate.action_hash(rollback_request),
        actor_id="safety-officer",
        private_key_pem=gate_priv_pem,
    )
    rollback_exec_digest = action_digest(
        rollback_request.scenario_id,
        rollback_request.catalog_action_id,
        rollback_request.target_scope,
        rollback_request.idempotency_key,
    )
    rollback_exec_approval = _sign_executor_approval(rollback_exec_digest, exec_priv)

    with SafeActionAuditStore(audit_db) as audit:
        workflow = SafeActionLiveWorkflow(
            gate=gate,
            audit=audit,
            executor=partial_then_rollback_executor,
        )
        rollback_result = workflow.rollback_after_failure(
            original_request,
            rollback_request,
            actor_role="executor",
            confidence=prepared["diagnosis_confidence"],
            pre_snapshot={"partial_failure_nodes": ["b"]},
            gate_approval=rollback_gate_approval,
            executor_approval=rollback_exec_approval,
        )
        assert rollback_result["status"] == "awaiting_verification"
        assert rollback_result["mutated"] is True
        assert rollback_result["resolution_eligible"] is False
        assert executed_actions == ["remove_scoped_db_reject", "reconcile_db01_safe_baseline"]
        assert audit.verify_chain(incident_id) is True
        audit_events = [ev["event_type"] for ev in audit.list_for_incident(incident_id)]
        assert "conditional_rollback_requested" in audit_events
        assert "conditional_rollback_result" in audit_events


def test_e2e_false_recovery_rejected_when_forbidden_probe_fails_or_window_short(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Branch 3 (RQ2): Health passes after execution, but forbidden probe or short stability window blocks RESOLVED."""
    evidence_db = tmp_path / "evidence-rq2.sqlite3"
    audit_db = tmp_path / "audit-rq2.sqlite3"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(evidence_db))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.delenv("VECTOR_DB_PATH", raising=False)
    _mock_con01_prometheus(monkeypatch)

    report = process_moodle_alert(_con01_alert())
    incident_id = report["incident_id"]
    with SQLiteEvidenceStore(evidence_db) as store:
        decisions = store.query(incident_id=incident_id, source="ai_decision")
        candidate_id = next(
            item.evidence_id
            for item in decisions
            if item.metadata["decision_kind"] == "remediation_candidate"
        )

    gate_priv, gate_priv_pem, gate_pub_pem = _generate_ed25519_pair()
    prepared = prepare_moodle_live_handoff(
        evidence_db=evidence_db,
        candidate_id=candidate_id,
        gate_public_key=gate_pub_pem,
        gate_actors={"safety-officer"},
    )
    request_id = prepared["request"]["request_id"]
    gate_approval = ApprovalRecord.create_ed25519(
        action_sha256=prepared["gate_action_sha256"],
        actor_id="safety-officer",
        private_key_pem=gate_priv_pem,
    )
    exec_priv, _, _ = _generate_ed25519_pair()
    executor_approval = _sign_executor_approval(prepared["executor_action_sha256"], exec_priv)

    execute_moodle_live_handoff(
        evidence_db=evidence_db,
        candidate_id=candidate_id,
        request_data=prepared["request"],
        gate_approval=gate_approval,
        executor_approval=executor_approval,
        gate_public_key=gate_pub_pem,
        gate_actors={"safety-officer"},
        audit_db=audit_db,
        kill_switch_enabled=False,
        executor=lambda **kwargs: {
            "status": "executed",
            "scenario_id": kwargs["scenario_id"],
            "completed_nodes": ["b"],
            "mutated": True,
        },
    )

    with SafeActionAuditStore(audit_db) as audit:
        receipt_hash = next(
            ev["event_hash"]
            for ev in audit.list_for_incident(incident_id)
            if ev["event_type"] == "live_executor_result"
        )

    verifier_priv, _, verifier_pub_pem = _generate_ed25519_pair()
    verifier_pub_path = tmp_path / "verifier-public.pem"
    verifier_pub_path.write_bytes(verifier_pub_pem)

    # Case 3a: Allowed HTTP probe passes, but forbidden probe (internet_to_postgresql_blocked) fails
    false_recovery_attestation = _build_signed_attestation(
        incident_id=incident_id,
        request_id=request_id,
        scenario_id="CON-01",
        receipt_event_hash=receipt_hash,
        verifier_private=verifier_priv,
        override_check=("internet_to_postgresql_blocked", False),
    )
    with pytest.raises(ValueError, match="failed contract probes"):
        accept_verifier_attestation(
            audit_db=audit_db,
            public_key_path=verifier_pub_path,
            request_id=request_id,
            attestation=false_recovery_attestation,
        )

    # Case 3b: All probes pass, but stability window is only 60s (< 120s required)
    short_window_attestation = _build_signed_attestation(
        incident_id=incident_id,
        request_id=request_id,
        scenario_id="CON-01",
        receipt_event_hash=receipt_hash,
        verifier_private=verifier_priv,
        sample_count=5,
        sample_interval_seconds=15,
    )
    with pytest.raises(ValueError, match="stability window is incomplete"):
        accept_verifier_attestation(
            audit_db=audit_db,
            public_key_path=verifier_pub_path,
            request_id=request_id,
            attestation=short_window_attestation,
        )

    # Confirm incident never transitioned to 'resolved' and audit chain remains intact
    status_now = get_runtime_verification_status(
        audit_db=audit_db, incident_id=incident_id, request_id=request_id
    )
    assert status_now["status"] == "verifying"
    with SafeActionAuditStore(audit_db) as audit:
        assert audit.verify_chain(incident_id) is True
        assert not any(
            ev["event_type"] == "live_verification_result"
            for ev in audit.list_for_incident(incident_id)
        )
