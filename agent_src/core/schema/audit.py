"""Audit contracts for state transitions in the unified AIOps core."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import IncidentStatus


class AuditEvent(BaseModel):
    event_id: str
    incident_id: str
    old_state: IncidentStatus
    new_state: IncidentStatus
    actor: str
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    run_id: str | None = None
    action_id: str | None = None
    scenario_id: str | None = None
    duration_ms: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class StageAuditEvent(BaseModel):
    """Append-only record for one completed or failed orchestration stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    incident_id: str
    run_id: str
    event_type: Literal["stage"] = "stage"
    stage: Literal[
        "observe", "triage", "diagnose", "plan", "gate_request", "execute", "verify", "failed"
    ]
    outcome: Literal["completed", "awaiting_approval", "denied", "failed"]
    actor: str
    evidence_refs: list[str] = Field(default_factory=list)
    action_id: str | None = None
    output_digest: str | None = None
    details: dict[str, str] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
