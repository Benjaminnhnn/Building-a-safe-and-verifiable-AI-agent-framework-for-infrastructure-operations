"""Controlled AIHS advisory binding to the existing Moodle staging handoff.

This module prepares an Operator review only. It never signs, executes, or
accepts a Verifier result. The existing handoff owns approval and Executor;
the Independent Verifier remains the only path to RESOLVED.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core.evidence.adapters import SafeLiveActionRequest
from core.moodle.aihs_contract import DecisionContext, MappingDefinition, adapt_aihs_output
from core.moodle.live_handoff import prepare_moodle_live_handoff
from core.safety.action_catalog import ActionCatalog
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_execution_gate import SafeExecutionGate
from core.safety.safe_executor_client import action_digest


def prepare_aihs_staging_handoff(
    raw_output: Any,
    *,
    context: DecisionContext,
    evidence_db: str | Path,
    mapping: MappingDefinition,
    candidate_id: str,
    gate_public_key: bytes,
    gate_actors: set[str],
    audit_db: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind AIHS to a fresh planner candidate, or return a logged block.

    A live-reviewed mapping is a prerequisite, never an authorization. The
    planner must independently produce the exact same action from fresh
    evidence. The returned request still needs action-bound Operator approval.
    """
    adapter = adapt_aihs_output(
        raw_output, context=context, evidence_db=evidence_db,
        mapping=mapping, now=now,
    )
    status, reason, prepared = "BLOCKED", adapter.reason_code, None
    rule = next(
        (entry for entry in mapping.actions if entry.aihs_action == adapter.aihs_action),
        None,
    )
    if adapter.status == "SHADOW_CANDIDATE":
        if (mapping.review_status != "reviewed_for_live" or rule is None
                or rule.status != "live_reviewed" or not rule.review_reference):
            reason = "mapping_not_reviewed_for_live"
        else:
            catalog_entry = ActionCatalog.load_moodle_catalog().lookup(adapter.catalog_action_id)
            if (rule.required_role != "executor" or catalog_entry is None
                    or catalog_entry.permission.read_only):
                reason = "not_reviewed_mutating_action"
            else:
                try:
                    candidate = prepare_moodle_live_handoff(
                        evidence_db=evidence_db, candidate_id=candidate_id,
                        gate_public_key=gate_public_key, gate_actors=gate_actors,
                    )
                    request = SafeLiveActionRequest.model_validate(candidate["request"])
                    if not _bound_to_aihs(request, candidate, adapter, context, candidate_id):
                        reason = "planner_candidate_mismatch"
                    else:
                        status, reason, prepared = "AWAITING_OPERATOR_APPROVAL", "matched_reviewed_plan", candidate
                except (KeyError, TypeError, ValueError, OSError):
                    reason = "trusted_handoff_unavailable"

    audit_payload = {
        "mode": "aihs_staging_preparation_no_dispatch",
        "incident_id": context.incident_id,
        "candidate_id_sha256": hashlib.sha256(candidate_id.encode()).hexdigest(),
        "model_version": context.model_version,
        "data_version": context.data_version,
        "mapping_version": mapping.mapping_version,
        "mapping_review_reference_sha256": (
            hashlib.sha256(rule.review_reference.encode()).hexdigest()
            if rule and rule.review_reference else None
        ),
        "input_sha256": hashlib.sha256(
            json.dumps(raw_output, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "aihs_action": adapter.aihs_action,
        "catalog_action_id": adapter.catalog_action_id,
        "target_scope": adapter.target_scope,
        "adapter_status": adapter.status,
        "adapter_reason": adapter.reason_code,
        "status": status,
        "reason": reason,
        "gate_preview_decision": prepared["gate_preview"]["decision"] if prepared else None,
        "gate_action_sha256": prepared["gate_action_sha256"] if prepared else None,
        "executor_action_sha256": prepared["executor_action_sha256"] if prepared else None,
        "execution_permitted": False,
        "dispatched": False,
        "resolution_eligible": False,
    }
    with SafeActionAuditStore(audit_db) as audit:
        event = audit.append(
            incident_id=context.incident_id,
            event_type="aihs_staging_preparation",
            payload=audit_payload,
        )
    result = {**audit_payload, "audit_event_id": event["event_id"], "audit_event_hash": event["event_hash"]}
    if prepared is not None:
        result["operator_handoff"] = prepared
    return result


def _bound_to_aihs(request: SafeLiveActionRequest, prepared: dict[str, Any],
                   adapter: Any, context: DecisionContext, candidate_id: str) -> bool:
    action = request.action
    expected_executor_digest = action_digest(
        request.scenario_id, request.catalog_action_id,
        request.target_scope, request.idempotency_key,
    )
    return (
        prepared.get("status") == "approval_required"
        and prepared.get("candidate_id") == candidate_id
        and prepared.get("incident_id") == context.incident_id
        and prepared.get("execution_permitted") is False
        and prepared.get("resolution_eligible") is False
        and prepared.get("gate_preview", {}).get("decision") == "REQUIRE_APPROVAL"
        and request.catalog_action_id == adapter.catalog_action_id
        and request.scenario_id == context.scenario_id
        and request.target_scope == adapter.target_scope
        and action.incident_id == context.incident_id
        and action.target_resource_id == adapter.target_resource_id
        and action.environment.value == context.environment
        and len(action.evidence_refs) == len(set(action.evidence_refs))
        and set(action.evidence_refs) == set(adapter.evidence_refs)
        and prepared.get("gate_action_sha256") == SafeExecutionGate.action_hash(request)
        and prepared.get("executor_action_sha256") == expected_executor_digest
    )
