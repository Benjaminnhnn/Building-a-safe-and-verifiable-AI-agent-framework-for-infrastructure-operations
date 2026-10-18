"""Resource domain model.

A Resource describes a deployable unit whose state the agent can observe,
diagnose, and (if approved) remediate.  Resources are read-only after
construction; the dependency graph and all policy checks reference them by
resource_id.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.agent_replay.domain.enums import Environment


class Resource(BaseModel):
    """Typed, immutable descriptor of an infrastructure resource.

    Fields
    ------
    resource_id : str
        Stable identifier.  Must be unique within a deployment topology and
        consistent across Evidence, Incident, and ActionPlan references.
        Examples: ``"moodle-app"``, ``"postgres-db"``, ``"efs-volume"``.
    name : str
        Human-readable display name.
    type : str
        Resource class string (e.g. ``"application"``, ``"database"``,
        ``"container"``, ``"host"``, ``"network"``, ``"volume"``).
        A free string is used instead of a separate enum so that new resource
        types can be introduced in the ground-truth catalog without a code
        change; policy rules that need type constraints enumerate specific
        values in their condition sets.
    environment : Environment
        Deployment environment.  Used by the Safety Gate to restrict execution
        to STAGING unless HUMAN_ONLY approval is obtained.
    host : str | None
        EC2 instance role or hostname where this resource runs.
    labels : dict[str, str]
        Arbitrary key-value metadata (Prometheus labels, Compose project,
        team, tier, …).  All values must be scalar strings.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_id: str = Field(
        ...,
        min_length=1,
        description="Stable, unique resource identifier.",
        examples=["moodle-app", "postgres-db"],
    )
    name: str = Field(..., min_length=1, description="Human-readable display name.")
    type: str = Field(
        ...,
        min_length=1,
        description="Resource class (application, database, container, host, network, volume, …).",
        examples=["application", "database", "container"],
    )
    environment: Environment
    host: str | None = Field(
        default=None,
        description="EC2 instance role / hostname that hosts this resource, if applicable.",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Arbitrary scalar string labels (Prometheus labels, team, tier, …).",
    )

    @model_validator(mode="after")
    def _validate_labels(self) -> Resource:
        for key, value in self.labels.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Resource label keys must be non-blank strings")
            if not isinstance(value, str):
                raise ValueError(
                    f"Resource label values must be strings; got {type(value).__name__} for key {key!r}"
                )
        return self

