"""Verify independent live-verifier attestations and persist them at runtime."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, field_validator

from core.safety.safe_action_audit import SafeActionAuditStore
from core.schema.common import IncidentStatus
from core.schema.incident import Incident
from core.schema.verification import VerificationResult
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS
from core.verification.state_machine import IncidentStateMachine

MAX_ATTESTATION_AGE_SECONDS = 120
MAX_FUTURE_SKEW_SECONDS = 30


class ProbeSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    checks: dict[str, StrictBool]

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("probe sample timestamp must be timezone-aware")
        return value


class VerifierAttestationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt
    incident_id: str
    request_id: str
    scenario_id: str
    receipt_event_hash: str
    signed_at: datetime
    verification: VerificationResult
    probe_samples: list[ProbeSample]

    @field_validator("signed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attestation timestamp must be timezone-aware")
        return value


class VerifierAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: VerifierAttestationPayload
    signature: str


class InvalidVerifierSignature(ValueError):
    """The independent verifier's signature is missing or invalid."""


def canonical_payload(payload: dict[str, Any] | VerifierAttestationPayload) -> bytes:
    value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_attestation_signature(
    attestation: VerifierAttestation, public_key_path: str | Path
) -> str:
    try:
        key_data = Path(public_key_path).read_bytes()
    except OSError:
        raise
    try:
        public_key = serialization.load_pem_public_key(key_data)
        signature = base64.b64decode(attestation.signature, validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise InvalidVerifierSignature("verifier public key or signature is invalid") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise InvalidVerifierSignature("verifier key must be Ed25519")
    try:
        public_key.verify(signature, canonical_payload(attestation.payload))
    except InvalidSignature as exc:
        raise InvalidVerifierSignature("verifier attestation signature did not match") from exc
    return hashlib.sha256(canonical_payload(attestation.payload)).hexdigest()


def _validate_attestation_payload(payload: VerifierAttestationPayload, request_id: str) -> None:
    now = datetime.now(timezone.utc)
    signed_at = payload.signed_at.astimezone(timezone.utc)
    age = (now - signed_at).total_seconds()
    if age > MAX_ATTESTATION_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("verifier attestation is stale or from the future")
    if (
        type(payload.version) is not int
        or payload.version != 1
        or payload.request_id != request_id
        or payload.scenario_id not in MOODLE_LIVE_CONTRACTS
        or len(payload.receipt_event_hash) != 64
        or any(char not in "0123456789abcdef" for char in payload.receipt_event_hash)
    ):
        raise ValueError("verifier attestation binding is invalid")

    contract = MOODLE_LIVE_CONTRACTS[payload.scenario_id]
    required = set().union(*(set(contract[group]) for group in ("allowed", "forbidden", "related")))
    samples = payload.probe_samples
    observations = payload.verification.stability_observations
    if len(samples) < 5 or len(samples) != len(observations):
        raise ValueError("verifier attestation lacks the required live stability samples")
    times = [sample.observed_at.astimezone(timezone.utc) for sample in samples]
    if times != sorted(times) or (times[-1] - times[0]).total_seconds() < 120:
        raise ValueError("verifier attestation stability window is incomplete")
    if any(not 0 < (later - earlier).total_seconds() <= 60 for earlier, later in zip(times, times[1:])):
        raise ValueError("verifier attestation sampling gap exceeds 60 seconds")
    if any(set(sample.checks) != required or not all(sample.checks.values()) for sample in samples):
        raise ValueError("verifier attestation contains missing or failed contract probes")
    if any(
        observation.observed_at.astimezone(timezone.utc) != sample_time
        or observation.healthy is not all(sample.checks[name] for name in contract["allowed"])
        or observation.simulated
        for observation, sample, sample_time in zip(observations, samples, times, strict=True)
    ):
        raise ValueError("verifier observations do not match the signed live samples")

    result = payload.verification
    if result.checked_at.tzinfo is None or result.checked_at.utcoffset() is None:
        raise ValueError("verifier result timestamp must be timezone-aware")
    checked_at = result.checked_at.astimezone(timezone.utc)
    if checked_at < times[-1] or (signed_at - checked_at).total_seconds() < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("verifier result timestamp does not follow its live probe samples")
    if (
        result.incident_id != payload.incident_id
        or result.simulated
        or result.verdict != "resolved"
        or not result.resolution_eligible
        or not result.health_passed
        or not result.communication_contract_passed
        or result.stability_seconds != int((times[-1] - times[0]).total_seconds())
    ):
        raise ValueError("verifier result is not eligible for resolution")
    for group in ("allowed", "forbidden", "related"):
        probes = getattr(result, f"{group}_probes")
        expected_names = set(contract[group])
        expected_outcomes = {
            name: all(sample.checks[name] for sample in samples)
            for name in expected_names
        }
        if {probe.name for probe in probes} != expected_names or any(
            probe.simulated or probe.passed is not expected_outcomes[probe.name]
            for probe in probes
        ):
            raise ValueError("verifier result does not satisfy the reviewed probe contract")


def _receipt_event(
    audit: SafeActionAuditStore, incident_id: str, request_id: str, scenario_id: str
) -> dict[str, Any]:
    events = audit.list_for_incident(incident_id)
    if not audit.verify_chain(incident_id):
        raise ValueError("runtime safe-action audit chain is invalid")
    receipts = [
        event for event in events
        if event.get("event_type") == "live_executor_result"
        and event.get("payload", {}).get("request_id") == request_id
        and event.get("payload", {}).get("scenario_id") == scenario_id
        and event.get("payload", {}).get("status") == "executed"
        and event.get("payload", {}).get("mutated") is True
    ]
    pending = [
        event for event in events
        if event.get("event_type") == "live_verification_pending"
        and event.get("payload", {}).get("request_id") == request_id
        and event.get("payload", {}).get("scenario_id") == scenario_id
    ]
    if len(receipts) != 1 or not pending:
        raise ValueError("runtime has no unique executed receipt awaiting verification")
    return receipts[0]


def accept_verifier_attestation(
    *,
    audit_db: str | Path,
    public_key_path: str | Path,
    request_id: str,
    attestation: VerifierAttestation,
) -> dict[str, Any]:
    digest = verify_attestation_signature(attestation, public_key_path)
    payload = attestation.payload
    _validate_attestation_payload(payload, request_id)

    with SafeActionAuditStore(audit_db) as audit:
        receipt = _receipt_event(audit, payload.incident_id, request_id, payload.scenario_id)
        if receipt.get("event_hash") != payload.receipt_event_hash:
            raise ValueError("verifier attestation does not bind the runtime executor receipt")

        current_status = audit.get_live_incident_status(payload.incident_id, request_id)
        previous_results = [
            event for event in audit.list_for_incident(payload.incident_id)
            if event.get("event_type") == "live_verification_result"
            and event.get("payload", {}).get("request_id") == request_id
        ]
        if current_status == "resolved":
            if any(event["payload"].get("attestation_sha256") == digest for event in previous_results):
                event = next(event for event in reversed(previous_results) if event["payload"].get("attestation_sha256") == digest)
                return {"status": "resolved", "event_id": event["event_id"], "event_hash": event["event_hash"], "idempotent": True}
            raise ValueError("request already has a different resolved verifier attestation")
        if current_status != "verifying":
            raise ValueError("runtime request is not awaiting independent verification")
        if any(event["payload"].get("attestation_sha256") == digest for event in previous_results):
            event = next(event for event in reversed(previous_results) if event["payload"].get("attestation_sha256") == digest)
            return {"status": "verifying", "event_id": event["event_id"], "event_hash": event["event_hash"], "idempotent": True}

        transition = IncidentStateMachine().resolve_verified(
            Incident(
                incident_id=payload.incident_id,
                fingerprint=f"safe-action:{request_id}",
                title=f"Moodle safe action {payload.scenario_id}",
                severity="warning",
                affected_resources=[payload.scenario_id],
                status=IncidentStatus.VERIFYING,
            ),
            payload.verification,
            reason=f"Independent live contract verification for request {request_id}",
            evidence_refs=[f"safe-action-request:{request_id}", payload.verification.verification_id],
        )
        event = audit.append_verification_result(
            incident_id=payload.incident_id,
            request_id=request_id,
            attestation_sha256=digest,
            payload={
                "request_id": request_id,
                "scenario_id": payload.scenario_id,
                "status": "resolved",
                "verification": payload.verification.model_dump(mode="json"),
                "state_transition": transition.model_dump(mode="json"),
                "probe_samples": [sample.model_dump(mode="json") for sample in payload.probe_samples],
                "verifier_attestation": attestation.model_dump(mode="json"),
                "attestation_sha256": digest,
            },
        )
        status = audit.get_live_incident_status(payload.incident_id, request_id)
        if status != "resolved":
            raise ValueError("runtime audit did not persist the verifier resolution")
        return {
            "status": status,
            "event_id": event["event_id"],
            "event_hash": event["event_hash"],
            "idempotent": event["_idempotent"],
        }


def get_runtime_verification_status(
    *, audit_db: str | Path, incident_id: str, request_id: str
) -> dict[str, Any]:
    with SafeActionAuditStore(audit_db) as audit:
        status = audit.get_live_incident_status(incident_id, request_id)
        events = [
            event for event in audit.list_for_incident(incident_id)
            if event.get("event_type") == "live_verification_result"
            and event.get("payload", {}).get("request_id") == request_id
        ]
        latest = events[-1] if events else None
        return {
            "incident_id": incident_id,
            "request_id": request_id,
            "status": status,
            "verification_event_id": latest["event_id"] if latest else None,
            "verification_event_hash": latest["event_hash"] if latest else None,
        }
