"""JSONL append-only audit log for Safety Gate decisions and pre-execution checks.

Invariants:
1. Every gate evaluation, rejection, approval verification, and pre-execution abort
   writes a record to the append-only JSONL log.
2. Required fields per entry:
   - timestamp: UTC timestamp
   - plan_hash: SHA-256 of the plan
   - policy_decision: Decision evaluated (ALLOW, REQUIRE_APPROVAL, DENY, HUMAN_ONLY)
   - reason_code: Machine-readable code explaining the decision
   - diff_summary: Concise dry-run impact diff summary
   - approver: Approver ID if approved/evaluated, else None
   - result: Final outcome (PERMITTED, REJECTED, ABORTED, EXECUTED)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEntry(BaseModel):
    """Immutable audit record entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    plan_hash: str = Field(..., min_length=1)
    policy_decision: str = Field(..., min_length=1)
    reason_code: str = Field(..., min_length=1)
    diff_summary: str = Field(default="")
    approver: str | None = None
    result: str = Field(..., min_length=1)


class SafetyAuditLogger:
    """Append-only audit logger supporting file persistence and in-memory querying."""

    def __init__(self, log_path: Path | str | None = None) -> None:
        self._log_path = Path(log_path) if log_path else None
        self._entries: list[AuditEntry] = []
        if self._log_path and self._log_path.exists():
            self._load_existing()

    def _load_existing(self) -> None:
        if not self._log_path:
            return
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    entry = AuditEntry.model_validate(data)
                    self._entries.append(entry)

    def log(
        self,
        *,
        plan_hash: str,
        policy_decision: str,
        reason_code: str,
        diff_summary: str | None = None,
        approver: str | None = None,
        result: str,
        timestamp: datetime | None = None,
    ) -> AuditEntry:
        """Append an audit record to the log."""
        entry = AuditEntry(
            timestamp=timestamp or datetime.now(timezone.utc),
            plan_hash=plan_hash,
            policy_decision=str(policy_decision),
            reason_code=str(reason_code),
            diff_summary=diff_summary or "",
            approver=approver,
            result=str(result),
        )
        self._entries.append(entry)
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                payload = entry.model_dump(mode="json")
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return entry

    def get_entries(self) -> list[AuditEntry]:
        """Return a copy of all audit entries."""
        return list(self._entries)

    def get_entries_for_plan(self, plan_hash: str) -> list[AuditEntry]:
        """Return all audit entries recorded for the given plan_hash."""
        return [e for e in self._entries if e.plan_hash == plan_hash]

    def has_rejection(self, plan_hash: str | None = None, reason_code: str | None = None) -> bool:
        """Check if any rejection/abort entry exists matching filters."""
        for e in self._entries:
            if plan_hash and e.plan_hash != plan_hash:
                continue
            if reason_code and e.reason_code != reason_code:
                continue
            if e.result in ("REJECTED", "ABORTED"):
                return True
        return False

