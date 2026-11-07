"""Operator-approved handoff from fresh Moodle evidence to the S3.3 executor."""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from core.evidence.adapters import SafeLiveActionRequest
from core.evidence.evidence_store import EvidenceIntegrityError, SQLiteEvidenceStore
from core.evidence.evidence_collectors import collect_shadow_evidence
from core.moodle.adapter import MoodlePlatformAdapter
from core.moodle.diagnosis import diagnose_moodle_alert
from core.moodle.planner import plan_moodle_s3_3_remediation
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_action_live import SafeActionLiveWorkflow
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.safety.safe_executor_client import action_digest


def _candidate_identity(candidate: dict[str, Any]) -> dict[str, Any]:
    """Compare the approved action while allowing newly collected evidence IDs."""
    result = deepcopy(candidate)
    result.pop("evidence_refs", None)
    action = result.get("action")
    if not isinstance(action, dict):
        raise EvidenceIntegrityError("candidate action is invalid")
    for field in ("action_id", "idempotency_key", "evidence_refs"):
        action.pop(field, None)
    return result


def _fresh_candidate(
    store: SQLiteEvidenceStore, candidate_id: str, *, refresh_if_stale: bool = False
) -> tuple[dict[str, Any], float, list[str]]:
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
    alert_records = [item for item in store.list_for_incident(record.incident_id) if item.source == "alertmanager" and item.metadata.get("signal")]
    for alert_record in alert_records:
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
    if len(matching_diagnoses) == 1:
        return candidate, float(matching_diagnoses[0].get("confidence", 0.0)), references
    if not refresh_if_stale or len(alert_records) != 1:
        raise EvidenceIntegrityError("candidate no longer matches fresh evidence and diagnosis")

    # The Operator may approve much later. Recollect read-only observations for
    # the original incident and require the same reviewed action and target.
    alert_record = alert_records[0]
    alert_name = str(alert_record.metadata["signal"])
    adapter = MoodlePlatformAdapter()
    alert = {"status": "firing", "labels": {"alertname": alert_name, "service": "moodle", "environment": "staging"}}
    collection = collect_shadow_evidence(
        alert, incident_id=record.incident_id, resource_id=alert_record.resource_id,
        evidence_store=store, prometheus_url=os.getenv("PROMETHEUS_URL"),
        queries=adapter.get_alert_queries(alert_name),
    )
    refreshed = [store.get_by_id(ref) for ref in collection["evidence_refs"]]
    if any(item is None or item.incident_id != record.incident_id for item in refreshed):
        raise EvidenceIntegrityError("refreshed evidence is missing or belongs to another incident")
    diagnosis = diagnose_moodle_alert(alert_name, refreshed)
    current = plan_moodle_s3_3_remediation(
        incident_id=record.incident_id, alert_name=alert_name, diagnosis=diagnosis, evidence=refreshed,
    )
    if current is None or _candidate_identity(current) != _candidate_identity(candidate):
        raise EvidenceIntegrityError("fresh evidence no longer supports the approved action")
    return candidate, float(diagnosis.get("confidence", 0.0)), list(current["evidence_refs"])


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
    """Return immutable request data and approval digests; never dispatch."""
    with SQLiteEvidenceStore(evidence_db) as store:
        candidate, confidence, _ = _fresh_candidate(store, candidate_id)
    request_id, idempotency_key = str(uuid.uuid4()), str(uuid.uuid4())
    request = _request(candidate, request_id=request_id, idempotency_key=idempotency_key)
    approval_mode = os.getenv("AIOPS_APPROVAL_MODE", "two_person")
    if approval_mode not in {"two_person", "operator"}:
        raise ValueError("unknown live approval mode")
    gate = SafeExecutionGate(
        approval_verification_key=gate_public_key,
        approved_actors=gate_actors,
        approval_digest_mode="executor" if approval_mode == "operator" else "full",
    )
    action_sha256 = gate.action_hash(request)
    # Read-only explanation for the approver: no approval is passed, so this can
    # never return ALLOW for an approval-required action or dispatch anything.
    gate_preview = gate.explain(request, actor_role="executor", confidence=confidence)
    return {
        "status": "approval_required",
        "gate_preview": gate_preview,
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
    gate_approval: ApprovalRecord | None, executor_approval: dict[str, str], gate_public_key: bytes,
    gate_actors: set[str], audit_db: str | Path, kill_switch_enabled: bool,
    executor: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Revalidate a prepared candidate and dispatch only with valid approval."""
    with SQLiteEvidenceStore(evidence_db) as store:
        candidate, confidence, current_evidence_refs = _fresh_candidate(store, candidate_id, refresh_if_stale=True)
    request = _request(
        candidate,
        request_id=str(request_data.get("request_id", "")),
        idempotency_key=str(request_data.get("idempotency_key", "")),
    )
    supplied = SafeLiveActionRequest.model_validate(request_data)
    if supplied != request:
        raise EvidenceIntegrityError("approval request differs from the current candidate")
    approval_mode = os.getenv("AIOPS_APPROVAL_MODE", "two_person")
    if approval_mode not in {"two_person", "operator"}:
        raise ValueError("unknown live approval mode")
    if approval_mode == "operator":
        if gate_approval is not None or not isinstance(executor_approval, dict):
            raise ValueError("operator mode accepts one operator signature")
        if executor_approval.get("actor_id") != "operator":
            raise ValueError("operator signature is required")
        gate_approval = ApprovalRecord.model_validate({
            "action_sha256": executor_approval.get("action_sha256"),
            "actor_id": executor_approval.get("actor_id"),
            "expires_at": executor_approval.get("expires_at"),
            "signature": "ed25519:" + str(executor_approval.get("signature") or ""),
        })
    gate = SafeExecutionGate(
        approval_verification_key=gate_public_key,
        approved_actors=gate_actors,
        kill_switch_enabled=kill_switch_enabled,
        approval_digest_mode="executor" if approval_mode == "operator" else "full",
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
            pre_snapshot={"candidate_id": candidate_id, "evidence_refs": candidate["evidence_refs"], "current_evidence_refs": current_evidence_refs},
            gate_approval=gate_approval,
            executor_approval=executor_approval,
        )
