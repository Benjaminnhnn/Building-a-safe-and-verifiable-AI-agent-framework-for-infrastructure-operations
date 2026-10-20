"""Operator-approved handoff from fresh Moodle evidence to the S3.3 executor."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from core.evidence.adapters import SafeLiveActionRequest
from core.evidence.evidence_store import EvidenceIntegrityError, SQLiteEvidenceStore
from core.moodle.diagnosis import diagnose_moodle_alert
from core.moodle.planner import plan_moodle_s3_3_remediation
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_action_live import SafeActionLiveWorkflow
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.safety.safe_executor_client import action_digest


def _fresh_candidate(store: SQLiteEvidenceStore, candidate_id: str) -> tuple[dict[str, Any], float]:
    store.verify_integrity()
    record = store.get_by_id(candidate_id)
    if (
        record is None
        or record.source != "ai_decision"
        or record.metadata.get("decision_kind") != "remediation_candidate"
    ):
        raise ValueError("remediation candidate was not found")
    candidate = json.loads(record.metadata["decision_json"])
    references = candidate.get("evidence_refs")
    if (
        not isinstance(references, list)
        or len(references) < 3
        or any(not isinstance(ref, str) or not ref for ref in references)
        or len(set(references)) != len(references)
    ):
        raise EvidenceIntegrityError("candidate evidence references are invalid")
    evidence = [store.get_by_id(ref) for ref in references]
    if any(item is None or item.incident_id != record.incident_id for item in evidence):
        raise EvidenceIntegrityError("candidate references missing or cross-incident evidence")
    matching_diagnoses = []
    for alert_record in store.list_for_incident(record.incident_id):
        if alert_record.source != "alertmanager":
            continue
        alert_name = alert_record.metadata.get("signal", "")
        diagnosis = diagnose_moodle_alert(alert_name, evidence)
        current = plan_moodle_s3_3_remediation(
            incident_id=record.incident_id,
            alert_name=alert_name,
            diagnosis=diagnosis,
            evidence=evidence,
        )
        if current == candidate:
            matching_diagnoses.append(diagnosis)
    if len(matching_diagnoses) != 1:
        raise EvidenceIntegrityError("candidate no longer matches fresh evidence and diagnosis")
    return candidate, float(matching_diagnoses[0].get("confidence", 0.0))


def _request(candidate: dict[str, Any], *, request_id: str, idempotency_key: str) -> SafeLiveActionRequest:
    action_data = dict(candidate["action"])
    action_data["idempotency_key"] = idempotency_key
    return SafeLiveActionRequest(
        request_id=request_id,
        idempotency_key=idempotency_key,
        catalog_action_id=candidate["catalog_action_id"],
        scenario_id=candidate["scenario_id"],
        target_scope=candidate["target_scope"],
        action=action_data,
        timeout_seconds=120,
    )


def prepare_moodle_live_handoff(
    *, evidence_db: str | Path, candidate_id: str, gate_public_key: bytes, gate_actors: set[str]
) -> dict[str, Any]:
    """Return immutable request data and both approval digests; never dispatch."""
    with SQLiteEvidenceStore(evidence_db) as store:
        candidate, confidence = _fresh_candidate(store, candidate_id)
    request_id, idempotency_key = str(uuid.uuid4()), str(uuid.uuid4())
    request = _request(candidate, request_id=request_id, idempotency_key=idempotency_key)
    gate = SafeExecutionGate(
        approval_verification_key=gate_public_key,
        approved_actors=gate_actors,
    )
    action_sha256 = gate.action_hash(request)
    return {
        "status": "approval_required",
        "incident_id": request.action.incident_id,
        "candidate_id": candidate_id,
        "request": request.model_dump(mode="json"),
        "diagnosis_confidence": confidence,
        "gate_action_sha256": action_sha256,
        "executor_action_sha256": action_digest(
            request.scenario_id,
            request.catalog_action_id,
            request.target_scope,
            request.idempotency_key,
        ),
        "execution_permitted": False,
        "resolution_eligible": False,
    }


def execute_moodle_live_handoff(
    *, evidence_db: str | Path, candidate_id: str, request_data: dict[str, Any],
    gate_approval: ApprovalRecord, executor_approval: dict[str, str], gate_public_key: bytes,
    gate_actors: set[str], audit_db: str | Path, kill_switch_enabled: bool,
    executor: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Revalidate a prepared candidate and dispatch only with both signatures."""
    with SQLiteEvidenceStore(evidence_db) as store:
        candidate, confidence = _fresh_candidate(store, candidate_id)
    request = _request(
        candidate,
        request_id=str(request_data.get("request_id", "")),
        idempotency_key=str(request_data.get("idempotency_key", "")),
    )
    supplied = SafeLiveActionRequest.model_validate(request_data)
    if supplied != request:
        raise EvidenceIntegrityError("approval request differs from the current candidate")
    gate = SafeExecutionGate(
        approval_verification_key=gate_public_key,
        approved_actors=gate_actors,
        kill_switch_enabled=kill_switch_enabled,
    )
    with SafeActionAuditStore(audit_db) as audit:
        workflow = SafeActionLiveWorkflow(
            gate=gate,
            audit=audit,
            **({"executor": executor} if executor is not None else {}),
        )
        return workflow.run(
            request,
            actor_role="executor",
            confidence=confidence,
            pre_snapshot={"candidate_id": candidate_id, "evidence_refs": candidate["evidence_refs"]},
            gate_approval=gate_approval,
            executor_approval=executor_approval,
        )
