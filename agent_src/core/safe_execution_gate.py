"""Fail-closed safety gate for the bounded S3.3 Moodle execution slice.

The gate deliberately authorizes no live mutation.  It validates a typed
dry-run request before it can reach a safe adapter and creates/verifies a
short-lived, action-bound approval only for requests that need one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import datetime, timedelta, timezone

from core.action_catalog import ActionCatalog
from core.adapters import SafeActionRequest, SafeLiveActionRequest
from pydantic import BaseModel, ConfigDict, Field


class GateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str  # ALLOW | REQUIRE_APPROVAL | DENY
    reason: str
    action_sha256: str
    approval_required: bool = False


class ApprovalRecord(BaseModel):
    """Approval bound to one canonical request and a maximum 15-minute TTL."""

    model_config = ConfigDict(extra="forbid")

    action_sha256: str
    actor_id: str
    expires_at: datetime
    signature: str

    @classmethod
    def create(
        cls, *, action_sha256: str, actor_id: str, signing_key: str, ttl_seconds: int = 600
    ) -> ApprovalRecord:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        payload = f"{action_sha256}:{actor_id}:{expires_at.isoformat()}"
        signature = hmac.new(signing_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return cls(
            action_sha256=action_sha256,
            actor_id=actor_id,
            expires_at=expires_at,
            signature=signature,
        )


class SafeExecutionGate:
    """Validate catalog, evidence, RBAC, timeout, approval and kill switch."""

    def __init__(
        self,
        *,
        approval_signing_key: str,
        approved_actors: set[str],
        kill_switch_enabled: bool = False,
        catalog: ActionCatalog | None = None,
    ) -> None:
        self._approval_signing_key = approval_signing_key
        self._approved_actors = approved_actors
        self._kill_switch_enabled = kill_switch_enabled
        self._catalog = catalog or ActionCatalog.load_moodle_s3_3_execution_catalog()
        self._seen_idempotency_keys: set[str] = set()

    @staticmethod
    def action_hash(request: SafeActionRequest | SafeLiveActionRequest) -> str:
        """Hash only the execution-relevant typed fields, never secrets."""
        payload = {
            "catalog_action_id": request.catalog_action_id,
            "scenario_id": request.scenario_id,
            "target_scope": request.target_scope,
            "incident_id": request.action.incident_id,
            "environment": request.action.environment.value,
            "evidence_refs": sorted(request.action.evidence_refs),
            "idempotency_key": request.idempotency_key,
            "timeout_seconds": request.timeout_seconds,
            "dry_run": request.dry_run,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def evaluate(
        self,
        request: SafeActionRequest | SafeLiveActionRequest,
        *,
        actor_role: str,
        confidence: float,
        approval: ApprovalRecord | None = None,
    ) -> GateVerdict:
        action_hash = self.action_hash(request)
        if self._kill_switch_enabled:
            return self._deny(action_hash, "kill switch is enabled")
        if not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return self._deny(action_hash, "confidence must be a finite value from 0 to 1")
        if request.idempotency_key in self._seen_idempotency_keys:
            return self._deny(action_hash, "idempotency key has already passed the gate")
        if len(set(request.action.evidence_refs)) < 3:
            return self._deny(action_hash, "at least three distinct evidence references are required")

        entry = self._catalog.lookup(request.catalog_action_id)
        if entry is None or request.timeout_seconds > entry.timeout_seconds:
            return self._deny(action_hash, "catalog action is unknown or requested timeout exceeds its limit")
        if not self._catalog.is_safe_execution_allowed(
            request.catalog_action_id,
            scenario_id=request.scenario_id,
            target_scope=request.target_scope,
            environment=request.action.environment.value,
            role=actor_role,
        ):
            return self._deny(action_hash, "request is outside the catalog, scope, environment, or RBAC boundary")
        if confidence < 0.65:
            return self._deny(action_hash, "diagnosis confidence is below 0.65")

        approval_required = confidence < 0.80 or entry.blast_radius != "LOW"
        if approval_required:
            if not self._valid_approval(approval, action_hash):
                return GateVerdict(
                    decision="REQUIRE_APPROVAL",
                    reason="valid, action-bound approval is required",
                    action_sha256=action_hash,
                    approval_required=True,
                )

        self._seen_idempotency_keys.add(request.idempotency_key)
        return GateVerdict(
            decision="ALLOW",
            reason="typed staging request passed catalog, evidence, RBAC, timeout and approval gates",
            action_sha256=action_hash,
            approval_required=approval_required,
        )

    def _valid_approval(self, approval: ApprovalRecord | None, action_hash: str) -> bool:
        if approval is None or approval.actor_id not in self._approved_actors:
            return False
        now = datetime.now(timezone.utc)
        if approval.expires_at <= now or approval.expires_at > now + timedelta(minutes=15):
            return False
        if not hmac.compare_digest(approval.action_sha256, action_hash):
            return False
        payload = f"{approval.action_sha256}:{approval.actor_id}:{approval.expires_at.isoformat()}"
        expected = hmac.new(
            self._approval_signing_key.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(approval.signature, expected)

    @staticmethod
    def _deny(action_hash: str, reason: str) -> GateVerdict:
        return GateVerdict(decision="DENY", reason=reason, action_sha256=action_hash)
