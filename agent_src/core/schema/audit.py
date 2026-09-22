"""Audit contracts for state transitions in the unified AIOps core."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

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
