"""Typed message contracts for the sequential AIOps pipeline.

Design invariants:
1. Every message envelope carries incident_id and evidence_ids.
2. All input/output models are typed according to Phase 1 domain models.
3. Immutability and extra-field protection to maintain schema integrity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field


class BaseRoleMessage(BaseModel):
    """Base message envelope carrying correlation identifiers across all stages."""

    model_config = ConfigDict(arbitrary_types_allowed=True, revalidate_instances="never", extra="forbid")

    incident_id: str = Field(..., min_length=1, description="Unique incident identifier.")
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="List of associated evidence record IDs propagated across stages.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the message creation.",
    )
