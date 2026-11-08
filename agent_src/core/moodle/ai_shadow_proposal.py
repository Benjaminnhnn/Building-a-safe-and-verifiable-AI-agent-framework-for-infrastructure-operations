"""Opt-in, read-only model proposal against the actual reviewed Moodle Gate.

The model is outside the alert and execution paths. Its text is untrusted and
can only populate a bounded intent that the Gate evaluates without approval.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from core.evidence.adapters import SafeLiveActionRequest
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_execution_gate import SafeExecutionGate
from core.safety.sanitization import sanitize_untrusted_data


class ModelIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=100)
    target_scope: str = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=300)


def evaluate_model_proposal(
    prepared: dict[str, Any], *, public_key: bytes, actors: set[str], audit_db: str,
    client: Any | None = None,
) -> dict[str, Any]:
    if os.getenv("AIOPS_AI_SHADOW_PROPOSALS_ENABLED", "false").lower() != "true":
        raise ValueError("model proposal shadow mode is disabled")
    if prepared.get("execution_permitted") is not False:
        raise ValueError("candidate is not a non-executable preparation")
    request = SafeLiveActionRequest.model_validate(prepared.get("request"))
    confidence = prepared.get("diagnosis_confidence")
    model = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
    key = os.getenv("GEMINI_API_KEY")
    if client is None and (not key or key.startswith("your_")):
        raise ValueError("model credentials are unavailable")
    # Only bounded, trusted diagnosis fields enter the prompt; no raw alert,
    # usernames, secrets, free-form logs or untrusted evidence snippets.
    context = {
        "scenario_id": request.scenario_id,
        "resource_class": request.action.target_resource_id,
        "environment": request.action.environment.value,
        "evidence_count": len(set(request.action.evidence_refs)),
        "diagnosis_confidence": confidence,
    }
    prompt = (
        "You are proposing one remediation for this Moodle staging incident. "
        "Independently choose an action identifier and target scope. "
        "Do not assume any proposal will be executed. Return only JSON with "
        "action_id, target_scope, rationale. Incident: "
        + json.dumps(context, sort_keys=True)
    )
    if client is None:
        client = genai.Client(api_key=key, http_options={"timeout": 10000})
    response = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.4, max_output_tokens=1024,
        ),
    )
    intent = ModelIntent.model_validate_json(response.text or "")
    # The typed action, evidence and target remain immutable. The model cannot
    # supply commands, parameters, rollback data, approval or an executor URL.
    proposed = request.model_copy(update={
        "catalog_action_id": intent.action_id,
        "target_scope": intent.target_scope,
    })
    gate = SafeExecutionGate(approval_verification_key=public_key, approved_actors=actors)
    decision = gate.explain(proposed, actor_role="executor", confidence=confidence)
    result = {
        "mode": "model_generated_shadow_actual_gate",
        "source": "model_generated",
        "model": model,
        "incident_id": prepared["incident_id"],
        "candidate_id": prepared["candidate_id"],
        "request_id": request.request_id,
        "scenario_id": request.scenario_id,
        "proposal": sanitize_untrusted_data(intent.model_dump(), max_chars=300),
        "model_response_id": str(getattr(response, "response_id", "") or "")[:100],
        "model_version": str(getattr(response, "model_version", "") or "")[:100],
        "gate_decision": decision["decision"],
        "gate_reason": decision["reason"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "response_sha256": hashlib.sha256((response.text or "").encode()).hexdigest(),
        "execution_permitted": False,
        "dispatched": False,
        "risk_adjudication": "pending_independent_review",
    }
    with SafeActionAuditStore(audit_db) as audit:
        event = audit.append(
            incident_id=request.action.incident_id,
            event_type="model_proposal_shadow_gate",
            payload=result,
        )
    result["audit_event_id"] = event["event_id"]
    result["audit_event_hash"] = event["event_hash"]
    return result
