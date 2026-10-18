from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from core.main import app
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.verifier_attestation import (
    VerifierAttestation,
    VerifierAttestationPayload,
    accept_verifier_attestation,
    canonical_payload,
    get_runtime_verification_status,
)
from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS


def _keys(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / "verifier-private.pem"
    public_path = tmp_path / "verifier-public.pem"
    private_path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return private, private_path, public_path


def _runtime_audit(path):
    with SafeActionAuditStore(path) as audit:
        receipt = audit.append(
            incident_id="inc-1",
            event_type="live_executor_result",
            payload={
                "request_id": "req-1", "scenario_id": "CON-01",
                "status": "executed", "mutated": True,
            },
        )
        audit.append(
            incident_id="inc-1",
            event_type="live_verification_pending",
            payload={"request_id": "req-1", "scenario_id": "CON-01", "resolution_eligible": False},
        )
    return receipt["event_hash"]


def _attestation(tmp_path, audit_db, *, signed_at=None, mutate=None):
    receipt_hash = _runtime_audit(audit_db)
    private, private_path, public_path = _keys(tmp_path)
    now = datetime.now(timezone.utc)
    contract = MOODLE_LIVE_CONTRACTS["CON-01"]
    checks = set().union(*(set(contract[group]) for group in ("allowed", "forbidden", "related")))
    samples = [
        {
            "observed_at": (now - timedelta(seconds=120 - 30 * index)).isoformat(),
            "checks": {name: True for name in checks},
        }
        for index in range(5)
    ]
    observations = [
        StabilityObservation(observed_at=item["observed_at"], healthy=True, simulated=False)
        for item in samples
    ]
    verification = VerificationResult(
        verification_id="verify-1",
        incident_id="inc-1",
        health_passed=True,
        communication_contract_passed=True,
        stability_seconds=120,
        stability_observations=observations,
        resolution_eligible=True,
        allowed_probes=[ProbeResult(name=name, passed=True, simulated=False, details="5/5") for name in contract["allowed"]],
        forbidden_probes=[ProbeResult(name=name, passed=True, simulated=False, details="5/5") for name in contract["forbidden"]],
        related_probes=[ProbeResult(name=name, passed=True, simulated=False, details="5/5") for name in contract["related"]],
        verdict="resolved",
        simulated=False,
        checked_at=now,
    )
    payload = {
        "version": 1,
        "incident_id": "inc-1",
        "request_id": "req-1",
        "scenario_id": "CON-01",
        "receipt_event_hash": receipt_hash,
        "signed_at": (signed_at or now).isoformat(),
        "verification": verification.model_dump(mode="json"),
        "probe_samples": samples,
    }
    if mutate:
        mutate(payload)
    typed_payload = VerifierAttestationPayload.model_validate(payload)
    signature = base64.b64encode(private.sign(canonical_payload(typed_payload))).decode("ascii")
    return VerifierAttestation(payload=typed_payload, signature=signature), private_path, public_path


def test_verifier_attestation_is_persisted_by_runtime_and_idempotent(tmp_path):
    audit_db = tmp_path / "audit.sqlite3"
    attestation, _, public_key = _attestation(tmp_path, audit_db)
    assert get_runtime_verification_status(
        audit_db=audit_db, incident_id="inc-1", request_id="req-1",
    )["status"] == "verifying"

    first = accept_verifier_attestation(
        audit_db=audit_db, public_key_path=public_key, request_id="req-1", attestation=attestation,
    )
    second = accept_verifier_attestation(
        audit_db=audit_db, public_key_path=public_key, request_id="req-1", attestation=attestation,
    )

    assert first["status"] == "resolved"
    assert first["idempotent"] is False
    assert second["event_id"] == first["event_id"]
    assert second["idempotent"] is True
    assert get_runtime_verification_status(
        audit_db=audit_db, incident_id="inc-1", request_id="req-1",
    )["status"] == "resolved"
    with SafeActionAuditStore(audit_db) as audit:
        results = [e for e in audit.list_for_incident("inc-1") if e["event_type"] == "live_verification_result"]
        assert len(results) == 1


def test_tampered_signature_does_not_resolve_runtime(tmp_path):
    audit_db = tmp_path / "audit.sqlite3"
    attestation, _, public_key = _attestation(tmp_path, audit_db)
    tampered = attestation.model_copy(update={"signature": base64.b64encode(b"x" * 64).decode("ascii")})

    with pytest.raises(ValueError, match="signature"):
        accept_verifier_attestation(
            audit_db=audit_db, public_key_path=public_key, request_id="req-1", attestation=tampered,
        )
    assert get_runtime_verification_status(
        audit_db=audit_db, incident_id="inc-1", request_id="req-1",
    )["status"] == "verifying"


def test_wrong_verifier_key_and_modified_signed_payload_are_rejected(tmp_path):
    audit_db = tmp_path / "audit.sqlite3"
    attestation, _, public_key = _attestation(tmp_path, audit_db)
    _, _, wrong_public_key = _keys(tmp_path / "other")

    with pytest.raises(ValueError, match="signature"):
        accept_verifier_attestation(
            audit_db=audit_db, public_key_path=wrong_public_key, request_id="req-1", attestation=attestation,
        )
    changed_payload = attestation.model_copy(update={
        "payload": attestation.payload.model_copy(update={"scenario_id": "DB-01"}),
    })
    with pytest.raises(ValueError, match="signature"):
        accept_verifier_attestation(
            audit_db=audit_db, public_key_path=public_key, request_id="req-1", attestation=changed_payload,
        )


def test_stale_signed_attestation_is_rejected(tmp_path):
    audit_db = tmp_path / "audit.sqlite3"
    stale = datetime.now(timezone.utc) - timedelta(seconds=121)
    attestation, _, public_key = _attestation(tmp_path, audit_db, signed_at=stale)

    with pytest.raises(ValueError, match="stale"):
        accept_verifier_attestation(
            audit_db=audit_db, public_key_path=public_key, request_id="req-1", attestation=attestation,
        )


def test_attestation_must_bind_runtime_receipt_and_request(tmp_path):
    audit_db = tmp_path / "audit.sqlite3"
    attestation, _, public_key = _attestation(
        tmp_path, audit_db, mutate=lambda payload: payload.update(receipt_event_hash="0" * 64),
    )

    with pytest.raises(ValueError, match="receipt"):
        accept_verifier_attestation(
            audit_db=audit_db, public_key_path=public_key, request_id="req-1", attestation=attestation,
        )

    with pytest.raises(ValueError, match="binding"):
        accept_verifier_attestation(
            audit_db=audit_db, public_key_path=public_key, request_id="another-request", attestation=attestation,
        )


def test_failed_or_incomplete_probe_samples_are_rejected(tmp_path):
    audit_db = tmp_path / "audit.sqlite3"

    def fail_probe(payload):
        payload["probe_samples"][2]["checks"]["moodle_app_b_container_healthy"] = False

    attestation, _, public_key = _attestation(tmp_path, audit_db, mutate=fail_probe)
    with pytest.raises(ValueError, match="failed contract probes"):
        accept_verifier_attestation(
            audit_db=audit_db, public_key_path=public_key, request_id="req-1", attestation=attestation,
        )


def test_verification_api_persists_and_reports_runtime_state(tmp_path, monkeypatch):
    audit_db = tmp_path / "audit.sqlite3"
    attestation, _, public_key = _attestation(tmp_path, audit_db)
    token_path = tmp_path / "api-token"
    token_path.write_text("t" * 64, encoding="ascii")
    monkeypatch.setenv("AIOPS_SAFE_ACTION_AUDIT_DB", str(audit_db))
    monkeypatch.setenv("AIOPS_SAFE_ACTION_API_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("AIOPS_VERIFIER_PUBLIC_KEY_FILE", str(public_key))

    client = TestClient(app)
    response = client.post(
        "/api/moodle/verifications/req-1", json=attestation.model_dump(mode="json"),
    )
    status = client.get(
        "/api/moodle/incidents/inc-1/requests/req-1/status",
        headers={"Authorization": f"Bearer {'t' * 64}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert status.status_code == 200
    assert status.json()["status"] == "resolved"
    assert status.json()["verification_event_id"] == response.json()["event_id"]
    with SafeActionAuditStore(audit_db, read_only=True) as audit:
        result = next(
            event for event in audit.list_for_incident("inc-1")
            if event["event_type"] == "live_verification_result"
        )
    assert result["payload"]["verifier_attestation"] == attestation.model_dump(mode="json")


def test_verification_api_fails_closed_when_public_key_is_missing(tmp_path, monkeypatch):
    attestation, _, _ = _attestation(tmp_path, tmp_path / "audit.sqlite3")
    monkeypatch.delenv("AIOPS_VERIFIER_PUBLIC_KEY_FILE", raising=False)
    client = TestClient(app)

    response = client.post(
        "/api/moodle/verifications/req-1", json=attestation.model_dump(mode="json"),
    )

    assert response.status_code == 503
