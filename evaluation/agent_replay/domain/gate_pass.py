"""GatePass value object issued exclusively by SafetyGate.

Invariants:
1. GatePass can only be created by SafetyGate after all 3 verification steps pass.
2. The constructor verifies an internal secret sentinel token (_SAFETY_GATE_TOKEN),
   preventing arbitrary instantiation from outside the SafetyGate.
3. Once constructed, GatePass is an immutable value object.
4. The Executor requires a valid GatePass and will refuse execution without it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from evaluation.agent_replay.domain.action_plan import ActionPlan
from evaluation.agent_replay.domain.enums import ActionType

# Internal sentinel token known only to SafetyGate within this package.
# External callers passing None, custom strings, or mock tokens will be rejected.
_SAFETY_GATE_TOKEN = object()


class GatePass:
    """Immutable value object authorizing execution of an ActionPlan.

    Constructor enforces that callers provide the internal sentinel token `_SAFETY_GATE_TOKEN`.
    Direct instantiation from outside SafetyGate raises PermissionError.
    """

    __slots__ = (
        "_plan_hash",
        "_target_resource_id",
        "_action_type",
        "_issued_at",
        "_approver",
        "_token",
        "_signature",
    )

    def __init__(
        self,
        *,
        plan_hash: str,
        target_resource_id: str,
        action_type: ActionType,
        issued_at: datetime | None = None,
        approver: str | None = None,
        signature: str = "",
        _token: object = None,
    ) -> None:
        if _token is not _SAFETY_GATE_TOKEN:
            raise PermissionError(
                "GatePass can only be constructed internally by SafetyGate after all 3 verification steps pass."
            )
        object.__setattr__(self, "_plan_hash", plan_hash)
        object.__setattr__(self, "_target_resource_id", target_resource_id)
        object.__setattr__(self, "_action_type", action_type)
        object.__setattr__(self, "_issued_at", issued_at or datetime.now(timezone.utc))
        object.__setattr__(self, "_approver", approver)
        object.__setattr__(self, "_token", _token)
        object.__setattr__(self, "_signature", signature)

    @property
    def plan_hash(self) -> str:
        return self._plan_hash

    @property
    def target_resource_id(self) -> str:
        return self._target_resource_id

    @property
    def action_type(self) -> ActionType:
        return self._action_type

    @property
    def issued_at(self) -> datetime:
        return self._issued_at

    @property
    def approver(self) -> str | None:
        return self._approver

    @property
    def signature(self) -> str:
        return self._signature

    def is_valid_token(self) -> bool:
        """Verify that the internal token matches the SafetyGate sentinel."""
        return self._token is _SAFETY_GATE_TOKEN

    def is_valid_for_plan(self, plan: ActionPlan) -> bool:
        """Check whether this GatePass matches the target plan."""
        return (
            self.is_valid_token()
            and self._plan_hash == plan.plan_hash
            and self._target_resource_id == plan.target_resource_id
            and self._action_type == plan.action_type
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("GatePass is an immutable value object and cannot be modified.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("GatePass attributes cannot be deleted.")

    def __repr__(self) -> str:
        short_hash = self._plan_hash[:8] if len(self._plan_hash) >= 8 else self._plan_hash
        return (
            f"GatePass(plan_hash={short_hash}..., "
            f"target={self._target_resource_id}, "
            f"action={self._action_type.value}, "
            f"approver={self._approver})"
        )

