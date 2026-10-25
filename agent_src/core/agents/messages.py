"""Typed inter-agent messages and their stage audit records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.schema.audit import StageAuditEvent

Stage = Literal["observe", "diagnose", "plan", "gate_request", "execute", "verify"]
Outcome = Literal["completed", "awaiting_approval", "denied", "failed"]


class AgentMessage(BaseModel):
    """Envelope shared by every stage: one incident, its evidence, and its action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    stage: Stage
    produced_by: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    action_id: str | None = None
    produced_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def digest(self) -> str:
        """Content digest excluding the production time, for replay comparison."""
        payload = self.model_dump(mode="json", exclude={"produced_at"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ObservationReport(AgentMessage):
    stage: Literal["observe"] = "observe"
    alert_name: str
    resource_id: str
    alert_evidence_id: str
    evidence_status: str
    evidence_sources: list[str] = Field(default_factory=list)
    collector_errors: list[str] = Field(default_factory=list)


class DiagnosisReport(AgentMessage):
    stage: Literal["diagnose"] = "diagnose"
    diagnosis: dict[str, Any]
    diagnosis_mode: str
    decided_by: str
    runbook_chunk_ids: list[str] = Field(default_factory=list)


class PlanProposal(AgentMessage):
    stage: Literal["plan"] = "plan"
    read_only_plan: dict[str, Any] | None = None
    remediation_candidate: dict[str, Any] | None = None
    disposition: dict[str, Any]
    confirmed_affected_resources: list[str] = Field(default_factory=list)
    potentially_affected_resources: list[str] = Field(default_factory=list)


class GateRequest(AgentMessage):
    stage: Literal["gate_request"] = "gate_request"
    candidate_id: str
    request_id: str
    gate_action_sha256: str
    executor_action_sha256: str


class ExecutionReport(AgentMessage):
    stage: Literal["execute"] = "execute"
    candidate_id: str
    request_id: str
    status: str
    execution_permitted: bool


class VerificationReport(AgentMessage):
    stage: Literal["verify"] = "verify"
    request_id: str
    status: str
    resolution_eligible: bool


def stage_event(message: AgentMessage, *, outcome: Outcome, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the audit event recording that ``message`` completed its stage."""
    digest = message.digest()
    event = StageAuditEvent(
        event_id="stage-" + hashlib.sha256(f"{message.run_id}:{message.stage}:{digest}:{message.produced_at.isoformat()}".encode()).hexdigest()[:20],
        incident_id=message.incident_id,
        run_id=message.run_id,
        stage=message.stage,
        outcome=outcome,
        actor=message.produced_by,
        evidence_refs=sorted(set(message.evidence_ids)),
        action_id=message.action_id,
        output_digest=digest,
        details={key: str(value) for key, value in (details or {}).items() if value is not None},
        occurred_at=message.produced_at,
    )
    return event.model_dump(mode="json")
