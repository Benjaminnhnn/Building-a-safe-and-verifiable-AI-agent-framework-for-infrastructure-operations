"""AIHS shadow bridge to the real Safety Gate; never dispatches actions.

The model output can choose only a reviewed mapping. A separately prepared,
dry-run typed request supplies all execution-relevant fields. No action or
approval is fabricated from the AIHS output.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from core.evidence.adapters import SafeActionRequest
from core.moodle.aihs_contract import DecisionContext, MappingDefinition, adapt_aihs_output
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_execution_gate import SafeExecutionGate


def evaluate_aihs_shadow(
    raw_output: Any,
    *,
    context: DecisionContext,
    evidence_db: str | Path,
    mapping: MappingDefinition,
    audit_db: str | Path,
    gate_public_key: bytes,
    gate_actors: set[str],
    reviewed_request: SafeActionRequest | None = None,
    trusted_confidence: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a fail-closed proposal/Gate preview with no Executor call.

    ``reviewed_request`` is created by the trusted incident planner, not AIHS.
    ``trusted_confidence`` is likewise external: the public prototype does not
    emit a calibrated confidence. Neither is silently invented here.
    """
    adapter = adapt_aihs_output(
        raw_output, context=context, evidence_db=evidence_db,
        mapping=mapping, now=now,
    )
    decision: dict[str, Any] | None = None
    disposition = "HUMAN_REVIEW"
    reason = adapter.reason_code
    request_hash: str | None = None

    if adapter.status == "SHADOW_CANDIDATE":
        if reviewed_request is None:
            reason = "missing_reviewed_request"
        elif (trusted_confidence is None or isinstance(trusted_confidence, bool)
              or not isinstance(trusted_confidence, (float, int))
              or not math.isfinite(trusted_confidence)
              or not 0.0 <= trusted_confidence <= 1.0):
            reason = "missing_trusted_confidence"
        elif not _matches_candidate(reviewed_request, adapter, context):
            reason = "reviewed_request_mismatch"
        else:
            gate = SafeExecutionGate(
                approval_verification_key=gate_public_key,
                approved_actors=gate_actors,
            )
            rule = next(item for item in mapping.actions if item.aihs_action == adapter.aihs_action)
            decision = gate.explain(
                reviewed_request, actor_role=rule.required_role,
                confidence=float(trusted_confidence),
            )
            request_hash = gate.action_hash(reviewed_request)
            disposition = "GATE_SHADOW_ONLY"
            reason = "gate_evaluated_without_dispatch"

    payload = {
        "mode": "aihs_shadow_actual_gate_no_dispatch",
        "incident_id": context.incident_id,
        "scenario_id": context.scenario_id,
        "model_version": context.model_version,
        "data_version": context.data_version,
        "mapping_version": mapping.mapping_version,
        "predicted_at": context.predicted_at.isoformat(),
        "input_sha256": hashlib.sha256(
            json.dumps(raw_output, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "aihs_action": adapter.aihs_action,
        "adapter_status": adapter.status,
        "adapter_reason": adapter.reason_code,
        "catalog_action_id": adapter.catalog_action_id,
        "target_scope": adapter.target_scope,
        "evidence_ref_hashes": sorted(
            hashlib.sha256(ref.encode()).hexdigest() for ref in adapter.evidence_refs
        ),
        "disposition": disposition,
        "reason": reason,
        "gate_decision": decision["decision"] if decision else None,
        "gate_reason": decision["reason"] if decision else None,
        "gate_action_sha256": request_hash,
        "execution_permitted": False,
        "dispatched": False,
        "resolution_eligible": False,
    }
    with SafeActionAuditStore(audit_db) as audit:
        event = audit.append(
            incident_id=context.incident_id,
            event_type="aihs_shadow_evaluation",
            payload=payload,
        )
    return {**payload, "audit_event_id": event["event_id"], "audit_event_hash": event["event_hash"]}


def _matches_candidate(request: SafeActionRequest, adapter: Any, context: DecisionContext) -> bool:
    action = request.action
    return (
        request.catalog_action_id == adapter.catalog_action_id
        and request.scenario_id == context.scenario_id
        and request.target_scope == adapter.target_scope
        and action.incident_id == context.incident_id
        and action.target_resource_id == adapter.target_resource_id
        and action.environment.value == context.environment
        and len(action.evidence_refs) == len(set(action.evidence_refs))
        and set(action.evidence_refs) == set(adapter.evidence_refs)
    )
