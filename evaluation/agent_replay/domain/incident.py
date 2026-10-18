"""Incident domain model.

An Incident is the central coordinating object of the AIOps pipeline.  It
links together the alert fingerprint (de-duplication key), the affected
resources, the collected evidence, and the current diagnosis.

Design constraints
------------------
* ``status`` is **not** mutated on this model directly.  The
  ``IncidentStateMachine`` in ``core.verification.state_machine`` owns all transitions and
  writes an append-only SHA-256 chained audit event for each one.
* This domain model records the *current* view of the incident; historical
  states are preserved only in the audit chain.
* ``diagnosis_id`` is set by the Diagnosis Agent after a successful rule
  match.  It is ``None`` until diagnosis completes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class IncidentStatus(str, Enum):
    """Valid lifecycle states of an Incident.

    Transitions are exclusively managed by ``IncidentStateMachine``.
    """

    OPEN = "open"
    TRIAGED = "triaged"
    PLANNED = "planned"
    GATED = "gated"
    EXECUTED = "executed"
    VERIFYING = "verifying"
    VERIFIED_DRY_RUN = "verified_dry_run"
    RESOLVED = "resolved"
    FAILED = "failed"
    ESCALATED = "escalated"


class Incident(BaseModel):
    """Current state of an active or historical incident.

    Fields
    ------
    incident_id : str
        Stable identifier, typically a SHA-based prefix of the alert batch
        fingerprint and scenario ID.
    alert_fingerprint : str
        De-duplication key as supplied by Alertmanager or computed from alert
        labels.  Used to prevent duplicate incidents from the same alert burst.
    status : IncidentStatus
        Current lifecycle status.  Must only be advanced by
        ``IncidentStateMachine.transition()`` or
        ``IncidentStateMachine.resolve_verified()``.
    resource_ids : list[str]
        Non-empty list of resource IDs directly affected by this incident.
        Derived from alert label mappings in the Observer stage.
    diagnosis_id : str | None
        Set by the Diagnosis Agent once a diagnosis record has been persisted.
        ``None`` while the incident is still in OPEN or TRIAGED state.
    evidence_ids : list[str]
        Ordered list of evidence IDs collected for this incident.  Populated
        incrementally by the Observer and shadow pipeline.
    created_at : datetime
        UTC timestamp when the incident was first created.
    updated_at : datetime
        UTC timestamp of the most recent state change.  Updated by the
        state machine on each transition.
    """

    model_config = ConfigDict(
        # Allow assignment for updated_at and diagnosis_id (mutable fields),
        # while status must go through the state machine — enforced at the
        # application layer, not here, to keep this model serialisation-friendly.
        validate_assignment=True,
        extra="forbid",
    )

    incident_id: str = Field(..., min_length=1)
    alert_fingerprint: str = Field(..., min_length=1)
    status: IncidentStatus = IncidentStatus.OPEN
    resource_ids: list[str] = Field(..., min_length=1)
    diagnosis_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

