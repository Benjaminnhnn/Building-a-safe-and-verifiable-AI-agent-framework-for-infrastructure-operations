"""Approval domain models, append-only store, and human gate integration.

Invariants:
1. ApprovalRecord binds plan_hash, approver, scope (action_type + target),
   expires_at, and decided_at.
2. Approval records are stored append-only (no updates, no deletions).
3. HumanGate accepts decisions strictly from authorized approvers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.agent_replay.domain.action_plan import ActionPlan
from evaluation.agent_replay.domain.enums import ActionType


# ---------------------------------------------------------------------------
# Scope & Approval Record Models
# ---------------------------------------------------------------------------


class ApprovalScope(BaseModel):
    """Scope restriction for an approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_type: ActionType
    target: str = Field(..., min_length=1, description="Target resource ID")

    def matches(self, action_type: ActionType, target: str) -> bool:
        return self.action_type == action_type and self.target == target


class ApprovalRecord(BaseModel):
    """Immutable approval record bound to a specific plan_hash and scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_hash: str = Field(..., min_length=64, max_length=64)
    approver: str = Field(..., min_length=1)
    scope: ApprovalScope
    expires_at: datetime
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_timestamps(self) -> ApprovalRecord:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (UTC)")
        if self.decided_at.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware (UTC)")
        if self.expires_at <= self.decided_at:
            raise ValueError("expires_at must be strictly later than decided_at")
        return self

    def is_expired(self, at: datetime | None = None) -> bool:
        current_time = at or datetime.now(timezone.utc)
        return current_time >= self.expires_at

    def matches_plan(self, plan: ActionPlan) -> tuple[bool, str]:
        if plan.plan_hash != self.plan_hash:
            return False, f"plan_hash mismatch: expected {self.plan_hash}, got {plan.plan_hash}"
        if not self.scope.matches(plan.action_type, plan.target_resource_id):
            return (
                False,
                f"scope mismatch: expected ({self.scope.action_type.value}, {self.scope.target}), "
                f"got ({plan.action_type.value}, {plan.target_resource_id})",
            )
        return True, ""


# ---------------------------------------------------------------------------
# Append-Only Approval Store
# ---------------------------------------------------------------------------


class ApprovalStore:
    """Append-only store for ApprovalRecord instances.

    Guarantees that records can only be appended and never modified or deleted.
    Can be backed by an append-only JSONL file or in-memory list.
    """

    def __init__(self, log_path: Path | str | None = None) -> None:
        self._records: list[ApprovalRecord] = []
        self._log_path = Path(log_path) if log_path else None
        if self._log_path and self._log_path.exists():
            self._load_from_file()

    def _load_from_file(self) -> None:
        if not self._log_path:
            return
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    record = ApprovalRecord.model_validate(data)
                    self._records.append(record)

    def append(self, record: ApprovalRecord) -> None:
        if not isinstance(record, ApprovalRecord):
            raise TypeError(f"record must be an ApprovalRecord, got {type(record).__name__}")
        self._records.append(record)
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def get_latest_for_plan(self, plan_hash: str) -> ApprovalRecord | None:
        for rec in reversed(self._records):
            if rec.plan_hash == plan_hash:
                return rec
        return None

    def list_all(self) -> list[ApprovalRecord]:
        return list(self._records)


# ---------------------------------------------------------------------------
# Human Gate
# ---------------------------------------------------------------------------


class UnauthorizedApproverError(PermissionError):
    """Raised when an unauthorized actor attempts to approve or reject a plan."""


@runtime_checkable
class ApprovalNotifier(Protocol):
    """Protocol for sending approval requests to human operators."""

    def send_approval_request(
        self,
        *,
        plan: ActionPlan,
        diff_summary: str,
        timeout_seconds: int,
    ) -> bool:
        ...


class TelegramApprovalNotifier:
    """Sends approval requests via Telegram using the existing telegram_bot utility."""

    def __init__(self, chat_id: str | None = None) -> None:
        self._chat_id = chat_id

    def send_approval_request(
        self,
        *,
        plan: ActionPlan,
        diff_summary: str,
        timeout_seconds: int,
    ) -> bool:
        try:
            from utils.telegram_bot import send_telegram_message
            text = (
                f"*⚠️ Human Approval Required*\n\n"
                f"*Incident*: `{plan.incident_id}`\n"
                f"*Target*: `{plan.target_resource_id}`\n"
                f"*Action*: `{plan.action_type.value}`\n"
                f"*Operation*: {plan.operation}\n"
                f"*Plan Hash*: `{plan.plan_hash[:16]}...`\n"
                f"*Diff Summary*: {diff_summary}\n"
                f"*TTL*: {timeout_seconds} seconds\n"
            )
            return bool(send_telegram_message(message=text, chat_id=self._chat_id))
        except Exception:
            return False


class HumanGate:
    """Manages approval requests and submissions for REQUIRE_APPROVAL plans.

    Rules:
    - Only authorized approvers can submit approval decisions.
    - Approvals are recorded into the append-only store.
    """

    def __init__(
        self,
        *,
        authorized_approvers: set[str],
        store: ApprovalStore | None = None,
        notifier: ApprovalNotifier | None = None,
    ) -> None:
        if not authorized_approvers:
            raise ValueError("authorized_approvers must contain at least one approver")
        self._authorized_approvers = set(authorized_approvers)
        self._store = store or ApprovalStore()
        self._notifier = notifier

    @property
    def authorized_approvers(self) -> set[str]:
        return set(self._authorized_approvers)

    @property
    def store(self) -> ApprovalStore:
        return self._store

    def is_authorized(self, approver: str) -> bool:
        return approver in self._authorized_approvers

    def request_approval(
        self,
        plan: ActionPlan,
        *,
        diff_summary: str = "",
        timeout_seconds: int = 900,
    ) -> dict[str, Any]:
        """Create and publish an approval request to human operators."""
        if not isinstance(plan, ActionPlan):
            raise TypeError("plan must be an instance of ActionPlan")

        notification_sent = False
        if self._notifier is not None:
            notification_sent = self._notifier.send_approval_request(
                plan=plan,
                diff_summary=diff_summary,
                timeout_seconds=timeout_seconds,
            )

        return {
            "status": "pending_approval",
            "plan_hash": plan.plan_hash,
            "target": plan.target_resource_id,
            "action_type": plan.action_type.value,
            "timeout_seconds": timeout_seconds,
            "notification_sent": notification_sent,
        }

    def submit_decision(
        self,
        *,
        plan_hash: str,
        approver: str,
        approved: bool,
        scope: ApprovalScope,
        ttl_seconds: int = 900,
    ) -> ApprovalRecord | None:
        """Submit an approval or rejection decision.

        Raises UnauthorizedApproverError if approver lacks authorization.
        If approved, saves to append-only store and returns ApprovalRecord.
        If rejected, returns None.
        """
        if not self.is_authorized(approver):
            raise UnauthorizedApproverError(
                f"Approver '{approver}' is not in authorized approver list: {sorted(self._authorized_approvers)}"
            )

        if not approved:
            return None

        now = datetime.now(timezone.utc)
        record = ApprovalRecord(
            plan_hash=plan_hash,
            approver=approver,
            scope=scope,
            decided_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._store.append(record)
        return record

