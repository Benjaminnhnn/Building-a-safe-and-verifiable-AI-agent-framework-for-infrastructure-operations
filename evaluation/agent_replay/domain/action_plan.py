"""ActionPlan domain model with typed rollback and stable plan_hash.

Design invariants
-----------------
1. ``plan_hash`` is a SHA-256 over the canonical JSON serialisation of every
   field *except* ``plan_hash`` itself.  The serialisation uses
   ``sort_keys=True`` and ``separators=(",", ":")`` so that dict key order
   never affects the hash.

2. The hash is computed automatically in the ``model_validator`` and stored in
   the ``plan_hash`` field; callers must not supply it.

3. ``Rollback`` is a discriminated union:
   - ``ReversibleRollback``  — the action can be undone; steps must be
     provided; max_rollback_seconds must be positive.
   - ``IrreversibleRollback`` — the action cannot be automatically undone;
     a non-blank human-readable reason is required so that operators
     understand the risk before approving.

4. ``ExpectedScope`` captures blast-radius intent at plan time.  The Safety
   Gate uses ``max_blast_radius`` and ``affected_resource_ids`` to validate
   that the proposed action stays within its declared scope.

5. ``ActionType.BLOCKED_UNRESTRICTED_SHELL`` is unconditionally rejected by
   ``model_validator`` so that even a misconfigured Planner cannot produce a
   valid ActionPlan for it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from evaluation.agent_replay.domain.enums import ActionType


# ---------------------------------------------------------------------------
# Blast-radius enumeration
# ---------------------------------------------------------------------------


class BlastRadius(str, Enum):
    """Declared scope of impact of the proposed action."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Expected scope
# ---------------------------------------------------------------------------


class ExpectedScope(BaseModel):
    """Explicit blast-radius declaration attached to an ActionPlan.

    The Safety Gate compares ``max_blast_radius`` and the length of
    ``affected_resource_ids`` against its configured thresholds before
    producing an ALLOW decision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    affected_resource_ids: Annotated[list[str], Field(min_length=1)]
    max_blast_radius: BlastRadius

    @model_validator(mode="after")
    def _validate_resource_ids(self) -> ExpectedScope:
        for rid in self.affected_resource_ids:
            if not isinstance(rid, str) or not rid.strip():
                raise ValueError(
                    "Each resource ID in ExpectedScope.affected_resource_ids "
                    "must be a non-blank string"
                )
        return self


# ---------------------------------------------------------------------------
# Rollback discriminated union
# ---------------------------------------------------------------------------


class ReversibleRollback(BaseModel):
    """Rollback plan for an action that can be automatically undone.

    ``steps`` lists the ordered remediation commands (Ansible task names,
    catalog action IDs, …).  At least one step is required so that the
    Executor has a concrete plan rather than an empty promise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["reversible"] = "reversible"
    steps: Annotated[list[str], Field(min_length=1)]
    max_rollback_seconds: StrictInt = Field(
        ...,
        gt=0,
        description="Maximum wall-clock seconds the rollback is expected to take.",
    )

    @model_validator(mode="after")
    def _validate_steps(self) -> ReversibleRollback:
        for step in self.steps:
            if not isinstance(step, str) or not step.strip():
                raise ValueError("Each rollback step must be a non-blank string")
        return self


class IrreversibleRollback(BaseModel):
    """Rollback plan for an action that cannot be automatically undone.

    A human-readable ``reason`` is mandatory so that approvers understand
    the risk they are accepting before signing an ApprovalRecord.
    Whitespace-only strings are rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["irreversible"] = "irreversible"
    reason: str = Field(
        ...,
        min_length=1,
        description="Human-readable explanation of why this action cannot be automatically reversed.",
    )

    @model_validator(mode="after")
    def _validate_reason_not_blank(self) -> IrreversibleRollback:
        if not self.reason.strip():
            raise ValueError("IrreversibleRollback.reason must not be blank or whitespace-only")
        return self


# Discriminated union — Pydantic selects the correct branch via the ``kind``
# literal field.
Rollback = ReversibleRollback | IrreversibleRollback


# ---------------------------------------------------------------------------
# ActionPlan
# ---------------------------------------------------------------------------

def _compute_plan_hash(data: dict) -> str:
    """Stable SHA-256 over sorted, compact JSON of *data* (plan_hash excluded)."""
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,  # datetime → ISO string, enums → their .value
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ActionPlan(BaseModel):
    """Typed, hash-stable action plan produced by the Planner Agent.

    Fields
    ------
    incident_id : str
        ID of the Incident this plan addresses.
    action_type : ActionType
        The closed-set action type.  ``BLOCKED_UNRESTRICTED_SHELL`` is
        unconditionally rejected.
    target_resource_id : str
        The single resource the action will mutate.  Must appear in
        ``expected_scope.affected_resource_ids``.
    operation : str
        Short, human-readable description of what will be done, e.g.
        ``"restart compose service moodle-app"``.
    preconditions : list[str]
        Conditions that must hold before the action may execute.
    evidence_ids : list[str]
        IDs of the Evidence records that support this plan.  At least one
        is required.
    expected_scope : ExpectedScope
        Declared blast radius and affected resources.
    expected_outcome : str
        What the agent expects to be true after successful execution.
    rollback : Rollback
        Either a ``ReversibleRollback`` (with steps) or an
        ``IrreversibleRollback`` (with a reason).
    requested_by : str
        Identity of the agent role that produced this plan
        (e.g. ``"planner_agent"``).
    created_at : datetime
        UTC timestamp when the plan was created.  Set automatically to
        ``datetime.now(timezone.utc)`` if not supplied.
    plan_hash : str
        SHA-256 over the canonical JSON of every other field.  Computed
        automatically; must not be supplied by the caller.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(..., min_length=1)
    action_type: ActionType
    target_resource_id: str = Field(..., min_length=1)
    operation: str = Field(..., min_length=1)
    preconditions: list[str] = Field(default_factory=list)
    evidence_ids: Annotated[list[str], Field(min_length=1)]
    expected_scope: ExpectedScope
    expected_outcome: str = Field(..., min_length=1)
    rollback: Rollback
    requested_by: str = Field(..., min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Computed by model_validator — must NOT be supplied by the caller.
    plan_hash: str = Field(default="", description="Computed SHA-256; do not set manually.")

    @model_validator(mode="after")
    def _compute_and_lock_hash(self) -> ActionPlan:
        # Reject the permanently-denied action type early.
        if self.action_type is ActionType.BLOCKED_UNRESTRICTED_SHELL:
            raise ValueError(
                "ActionType.BLOCKED_UNRESTRICTED_SHELL is not a valid plan action; "
                "unrestricted shell execution is permanently denied."
            )

        # Validate target is within declared scope.
        if self.target_resource_id not in self.expected_scope.affected_resource_ids:
            raise ValueError(
                f"target_resource_id {self.target_resource_id!r} is not listed in "
                f"expected_scope.affected_resource_ids {self.expected_scope.affected_resource_ids!r}"
            )

        # Build the payload for hashing — exclude plan_hash itself.
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        computed_hash = _compute_plan_hash(payload)

        # Reject if plan_hash was set by the caller (could indicate tampering).
        if self.plan_hash:
            if self.plan_hash != computed_hash:
                raise ValueError(
                    "plan_hash must not be supplied by the caller; it is computed automatically."
                )
            return self

        # model_config=frozen requires object.__setattr__ for internal mutation
        # during validation.
        object.__setattr__(self, "plan_hash", computed_hash)
        return self
