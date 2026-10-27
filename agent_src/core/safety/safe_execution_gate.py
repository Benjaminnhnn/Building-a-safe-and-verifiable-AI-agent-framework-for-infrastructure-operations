"""Fail-closed gate for bounded, catalogued Moodle staging actions.

The gate evaluates requests but never executes them. Live execution is a
separate caller path requiring both this ALLOW decision and the executor's
independent action-bound approval.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import base64
from threading import Lock
from datetime import datetime, timedelta, timezone

from core.safety.action_catalog import ActionCatalog, CatalogEntry
from core.evidence.adapters import SafeActionRequest, SafeLiveActionRequest
from core.platform_adapter import PlatformAdapter, PlatformAdapterRegistry
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key


class GateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["ALLOW", "REQUIRE_APPROVAL", "DENY"]
    reason: str
    action_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_required: StrictBool = False


class ApprovalRecord(BaseModel):
    """Approval bound to one canonical request and a maximum 15-minute TTL."""

    model_config = ConfigDict(extra="forbid")

    action_sha256: StrictStr
    actor_id: StrictStr
    expires_at: datetime
    signature: StrictStr

    @classmethod
    def create(
        cls, *, action_sha256: str, actor_id: str, signing_key: str, ttl_seconds: int = 600
    ) -> ApprovalRecord:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900:
            raise ValueError("approval TTL must be an integer between 1 and 900 seconds")
        if not action_sha256 or not actor_id or not signing_key:
            raise ValueError("approval digest, actor, and signing key are required")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        payload = f"{action_sha256}:{actor_id}:{expires_at.isoformat()}"
        signature = hmac.new(signing_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return cls(
            action_sha256=action_sha256,
            actor_id=actor_id,
            expires_at=expires_at,
            signature=signature,
        )

    @classmethod
    def create_ed25519(
        cls, *, action_sha256: str, actor_id: str, private_key_pem: bytes, ttl_seconds: int = 600
    ) -> ApprovalRecord:
        """Create an approval for an operator-held Ed25519 private key."""
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900:
            raise ValueError("approval TTL must be an integer between 1 and 900 seconds")
        if not action_sha256 or not actor_id or not private_key_pem:
            raise ValueError("approval digest, actor, and private key are required")
        key = load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("approval private key must use Ed25519")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        payload = f"{action_sha256}:{actor_id}:{expires_at.isoformat(timespec='seconds').replace('+00:00', 'Z')}"
        signature = base64.b64encode(key.sign(payload.encode())).decode("ascii")
        return cls(
            action_sha256=action_sha256,
            actor_id=actor_id,
            expires_at=expires_at.replace(microsecond=0),
            signature=f"ed25519:{signature}",
        )


class SafeExecutionGate:
    """Validate catalog, evidence, RBAC, timeout, approval and kill switch."""

    def __init__(
        self,
        *,
        approval_signing_key: str | None = None,
        approval_verification_key: bytes | None = None,
        approved_actors: set[str],
        kill_switch_enabled: bool = False,
        catalog: ActionCatalog | None = None,
        rollback_catalog: ActionCatalog | None = None,
        scope_resources: dict[str, set[str]] | None = None,
        resource_types: dict[str, str] | None = None,
        adapter: PlatformAdapter | None = None,
    ) -> None:
        if bool(approval_signing_key) == bool(approval_verification_key):
            raise ValueError("configure exactly one HMAC signing key or Ed25519 verification key")
        self._approval_signing_key = approval_signing_key
        verification_key = (
            load_pem_public_key(approval_verification_key)
            if approval_verification_key is not None
            else None
        )
        if verification_key is not None and not isinstance(verification_key, Ed25519PublicKey):
            raise ValueError("approval verification key must use Ed25519")
        self._approval_verification_key = verification_key
        self._approved_actors = approved_actors
        self._kill_switch_enabled = kill_switch_enabled

        adapter = adapter or PlatformAdapterRegistry.get("moodle")
        if catalog is not None:
            self._catalog = catalog
        elif adapter is not None:
            self._catalog = adapter.get_action_catalog()
        else:
            self._catalog = ActionCatalog.load_full_catalog()

        # A reciprocal rollback must be reviewed in the same catalog that
        # authorises the action; the Moodle+ERPNext catalog has no BIZ inverses.
        self._rollback_catalog = rollback_catalog or self._catalog

        if scope_resources is not None:
            self._scope_resources = scope_resources
        elif adapter is not None:
            self._scope_resources = adapter.get_target_scopes()
        else:
            self._scope_resources = {}

        if resource_types is not None:
            self._resource_types = resource_types
        elif adapter is not None:
            self._resource_types = adapter.get_resource_types()
        else:
            self._resource_types = {}

        self._seen_idempotency_keys: dict[str, str] = {}
        self._idempotency_lock = Lock()

    @staticmethod
    def action_hash(request: SafeActionRequest | SafeLiveActionRequest) -> str:
        """Hash only the execution-relevant typed fields, never secrets."""
        payload = {
            "action_id": request.action.action_id,
            "catalog_action_id": request.catalog_action_id,
            "typed_action": request.action.action_type.value,
            "target_resource_id": request.action.target_resource_id,
            "reason": request.action.reason,
            "preconditions": request.action.preconditions,
            "expected_outcome": request.action.expected_outcome,
            "scenario_id": request.scenario_id,
            "target_scope": request.target_scope,
            "incident_id": request.action.incident_id,
            "environment": request.action.environment.value,
            "evidence_refs": sorted(request.action.evidence_refs),
            "reversible": request.action.reversible,
            "requires_approval": request.action.requires_approval,
            "rollback_plan": {
                "available": request.action.rollback_plan.available,
                "rollback_action_id": request.action.rollback_plan.rollback_action_id,
                "method": request.action.rollback_plan.method,
                "expected_duration_seconds": request.action.rollback_plan.expected_duration_seconds,
            },
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
        request_model = (
            SafeLiveActionRequest
            if isinstance(request, SafeLiveActionRequest)
            else SafeActionRequest
        )
        try:
            request = request_model.model_validate(
                request.model_dump(mode="python", warnings=False)
            )
            if approval is not None:
                approval = ApprovalRecord.model_validate(
                    approval.model_dump(mode="python", warnings=False)
                )
        except (AttributeError, TypeError, ValueError, ValidationError):
            return self._deny("0" * 64, "request or approval failed schema validation")

        action_hash = self.action_hash(request)
        if self._kill_switch_enabled:
            return self._deny(action_hash, "kill switch is enabled")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return self._deny(action_hash, "confidence must be a finite value from 0 to 1")
        with self._idempotency_lock:
            if request.idempotency_key in self._seen_idempotency_keys:
                return self._deny(action_hash, "idempotency key has already passed the gate")
        if len(set(request.action.evidence_refs)) < 3:
            return self._deny(action_hash, "at least three distinct evidence references are required")
        if request.action.parameters:
            return self._deny(action_hash, "caller parameters are not supported by the fixed staging executor")

        entry = self._catalog.lookup(request.catalog_action_id)
        if entry is None or request.timeout_seconds > entry.timeout_seconds:
            return self._deny(action_hash, "catalog action is unknown or requested timeout exceeds its limit")
        if entry.rollback_action and not self._has_reviewed_inverse(entry):
            return self._deny(action_hash, "catalog rollback action is missing or not a reciprocal staging mutation")
        if request.action.reversible != entry.is_reversible:
            return self._deny(action_hash, "typed action reversibility does not match the reviewed catalog")
        if not entry.permission.read_only and (
            not entry.is_reversible
            or not entry.rollback_action
            or request.action.rollback_plan.rollback_action_id != entry.rollback_action
            or not request.action.rollback_plan.available
            or not request.action.rollback_plan.method
            or not request.action.rollback_plan.method.strip()
        ):
            return self._deny(action_hash, "mutating action lacks a catalogued reversible rollback plan")
        expected_type = entry.action_type
        if expected_type is None or request.action.action_type != expected_type:
            return self._deny(action_hash, "typed action does not match the reviewed catalog action")
        if not self._catalog.is_safe_execution_allowed(
            request.catalog_action_id,
            scenario_id=request.scenario_id,
            target_scope=request.target_scope,
            environment=request.action.environment.value,
            role=actor_role,
        ):
            return self._deny(action_hash, "request is outside the catalog, scope, environment, or RBAC boundary")
        if request.action.target_resource_id not in self._scope_resources.get(request.target_scope, set()):
            return self._deny(action_hash, "target resource is outside the reviewed target scope")
        target_type = self._resource_types.get(request.action.target_resource_id)
        if target_type is None or not any(
            item.value == target_type for item in entry.valid_target_resource_types
        ):
            return self._deny(action_hash, "target resource class is not allowed by the catalog")
        if confidence < 0.65:
            return self._deny(action_hash, "diagnosis confidence is below 0.65")

        approval_required = (
            request.action.requires_approval
            or bool(entry.default_requires_approval)
            or confidence < 0.80
            or entry.blast_radius != "LOW"
        )
        if approval_required:
            if not self._valid_approval(approval, action_hash):
                return GateVerdict(
                    decision="REQUIRE_APPROVAL",
                    reason="valid, action-bound approval is required",
                    action_sha256=action_hash,
                    approval_required=True,
                )

        # Recheck and reserve atomically: concurrent requests with the same key
        # must not both receive ALLOW.
        with self._idempotency_lock:
            if request.idempotency_key in self._seen_idempotency_keys:
                return self._deny(action_hash, "idempotency key has already passed the gate")
            self._seen_idempotency_keys[request.idempotency_key] = action_hash
        return GateVerdict(
            decision="ALLOW",
            reason="typed staging request passed catalog, evidence, RBAC, timeout and approval gates",
            action_sha256=action_hash,
            approval_required=approval_required,
        )

    def release_before_execution(
        self,
        request: SafeActionRequest | SafeLiveActionRequest,
        verdict: GateVerdict,
    ) -> bool:
        """Release a gate reservation only for this exact, not-dispatched request."""
        expected_hash = self.action_hash(request)
        if verdict.decision != "ALLOW" or not hmac.compare_digest(verdict.action_sha256, expected_hash):
            return False
        with self._idempotency_lock:
            if self._seen_idempotency_keys.get(request.idempotency_key) != expected_hash:
                return False
            del self._seen_idempotency_keys[request.idempotency_key]
            return True

    def _valid_approval(self, approval: ApprovalRecord | None, action_hash: str) -> bool:
        if approval is None or approval.actor_id not in self._approved_actors:
            return False
        if approval.expires_at.tzinfo is None or approval.expires_at.utcoffset() is None:
            return False
        now = datetime.now(timezone.utc)
        if approval.expires_at <= now or approval.expires_at > now + timedelta(minutes=15):
            return False
        if not hmac.compare_digest(approval.action_sha256, action_hash):
            return False
        payload = f"{approval.action_sha256}:{approval.actor_id}:{approval.expires_at.isoformat()}"
        if self._approval_verification_key is not None:
            if not approval.signature.startswith("ed25519:"):
                return False
            try:
                signature = base64.b64decode(approval.signature.removeprefix("ed25519:"), validate=True)
                signed_at = approval.expires_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                signed_payload = f"{approval.action_sha256}:{approval.actor_id}:{signed_at}".encode()
                self._approval_verification_key.verify(signature, signed_payload)
                return True
            except (ValueError, InvalidSignature):
                return False
        if self._approval_signing_key is None:
            return False
        expected = hmac.new(self._approval_signing_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(approval.signature, expected)

    def _has_reviewed_inverse(self, entry: CatalogEntry) -> bool:
        rollback_id = entry.rollback_action
        rollback = self._rollback_catalog.lookup(rollback_id) if rollback_id else None
        return bool(
            rollback
            and not rollback.permission.read_only
            and rollback.permission.required_role == entry.permission.required_role
            and "staging" in rollback.permission.environment_scope
            and rollback.adapter == entry.adapter
            and rollback.is_reversible
            and rollback.rollback_action == entry.action_id
        )

    @staticmethod
    def _deny(action_hash: str, reason: str) -> GateVerdict:
        return GateVerdict(decision="DENY", reason=reason, action_sha256=action_hash)
