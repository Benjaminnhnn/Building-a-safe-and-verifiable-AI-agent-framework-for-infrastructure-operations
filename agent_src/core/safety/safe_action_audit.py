"""Append-only audit and dry-run rollback orchestration for S3.3 safe actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.evidence.adapters import ActionResult, SafeActionRequest, sanitize_adapter_output
from core.safety.sanitization import sanitize_log_excerpt
from core.safety.safe_execution_gate import GateVerdict, SafeExecutionGate
from pydantic import ValidationError

VERIFIER_PUBLIC_KEY_ENV = "AIOPS_VERIFIER_PUBLIC_KEY_FILE"

_SENSITIVE_KEY = re.compile(
    r"password|secret|token|private.?key|access.?key|api.?key|authorization|"
    r"credential|connection.?string|database.?url|dsn",
    re.IGNORECASE,
)


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def redact_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Redact prohibited keys recursively before a snapshot enters the audit log."""
    def redact(item: Any, key: str = "") -> Any:
        if _SENSITIVE_KEY.search(key):
            return "[REDACTED]"
        if isinstance(item, dict):
            return {str(k): redact(v, str(k)) for k, v in item.items()}
        if isinstance(item, list):
            return [redact(v) for v in item]
        if isinstance(item, str):
            sanitized = sanitize_adapter_output(item)
            return sanitize_log_excerpt(sanitized, max_chars=max(1, len(sanitized)))
        return item

    return redact(value)


class SafeActionAuditStore:
    """Per-incident hash-chained, append-only audit store."""

    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        verifier_public_key_path: str | Path | None = None,
    ) -> None:
        # Used only to re-verify the Independent Verifier's signature when a
        # request's status is computed. Without a key no request can be
        # reported as "resolved": the audit rows alone are never proof.
        self._verifier_public_key_path = verifier_public_key_path
        if read_only:
            database_path = Path(path).resolve()
            if not database_path.is_file():
                raise FileNotFoundError(database_path)
            self._connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
            self._connection.execute("PRAGMA query_only = ON")
        else:
            self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        if not read_only:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS safe_action_audit (
                    event_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    previous_hash TEXT, event_hash TEXT NOT NULL, canonical_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_safe_action_audit_incident
                    ON safe_action_audit(incident_id, occurred_at, event_id);
                CREATE TRIGGER IF NOT EXISTS safe_action_audit_no_update
                    BEFORE UPDATE ON safe_action_audit BEGIN
                    SELECT RAISE(ABORT, 'safe action audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS safe_action_audit_no_delete
                    BEFORE DELETE ON safe_action_audit BEGIN
                    SELECT RAISE(ABORT, 'safe action audit is append-only'); END;
                """
            )
            self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SafeActionAuditStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def append(self, *, incident_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        # BEGIN IMMEDIATE takes the write lock before the previous hash is read,
        # so two writers can never chain from the same predecessor.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            previous = self._connection.execute(
                "SELECT event_hash FROM safe_action_audit WHERE incident_id = ? ORDER BY rowid DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            event = {
                "event_id": str(uuid.uuid4()), "incident_id": incident_id, "event_type": event_type,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "previous_hash": previous["event_hash"] if previous else None,
                "payload": redact_snapshot(payload),
            }
            event_hash = hashlib.sha256(_canonical(event).encode()).hexdigest()
            event["event_hash"] = event_hash
            self._connection.execute(
                "INSERT INTO safe_action_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event["event_id"], incident_id, event_type, event["occurred_at"], event["previous_hash"], event_hash, _canonical(event)),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return event

    def append_verification_result(
        self,
        *,
        incident_id: str,
        request_id: str,
        attestation_sha256: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically append one verifier result for an executed request."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if not self.verify_chain(incident_id):
                raise ValueError("safe-action audit chain is invalid")
            events = self.list_for_incident(incident_id)
            existing = next((
                event for event in reversed(events)
                if event["event_type"] == "live_verification_result"
                and event["payload"].get("request_id") == request_id
                and event["payload"].get("attestation_sha256") == attestation_sha256
            ), None)
            if existing is not None:
                self._connection.rollback()
                return {**existing, "_idempotent": True}
            if self.get_live_incident_status(incident_id, request_id) != "verifying":
                raise ValueError("runtime request is no longer awaiting verification")

            previous = self._connection.execute(
                "SELECT event_hash FROM safe_action_audit WHERE incident_id = ? ORDER BY rowid DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            event = {
                "event_id": str(uuid.uuid4()),
                "incident_id": incident_id,
                "event_type": "live_verification_result",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "previous_hash": previous["event_hash"] if previous else None,
                "payload": redact_snapshot(payload),
            }
            event["event_hash"] = hashlib.sha256(_canonical(event).encode()).hexdigest()
            self._connection.execute(
                "INSERT INTO safe_action_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event["event_id"], incident_id, event["event_type"], event["occurred_at"],
                    event["previous_hash"], event["event_hash"], _canonical(event),
                ),
            )
            self._connection.commit()
            return {**event, "_idempotent": False}
        except Exception:
            self._connection.rollback()
            raise

    def list_for_incident(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT canonical_json FROM safe_action_audit WHERE incident_id = ? ORDER BY rowid", (incident_id,)
        ).fetchall()
        return [json.loads(row["canonical_json"]) for row in rows]

    def verify_chain(self, incident_id: str) -> bool:
        """Return whether the incident's chain is intact; malformed rows fail closed."""
        previous_hash: str | None = None
        try:
            events = self.list_for_incident(incident_id)
            for event in events:
                if not isinstance(event, dict):
                    return False
                stored_hash = event.pop("event_hash", None)
                if (
                    not isinstance(stored_hash, str)
                    or event.get("previous_hash") != previous_hash
                    or event.get("incident_id") != incident_id
                    or hashlib.sha256(_canonical(event).encode()).hexdigest() != stored_hash
                ):
                    return False
                previous_hash = stored_hash
        except (KeyError, TypeError, ValueError, AttributeError, sqlite3.Error):
            return False
        return True

    def _resolve_verifier_key(self, override: str | Path | None) -> Path | None:
        candidate = override or self._verifier_public_key_path or os.environ.get(VERIFIER_PUBLIC_KEY_ENV)
        return Path(candidate) if candidate else None

    def _attestation_is_authentic(
        self,
        *,
        incident_id: str,
        request_id: str,
        payload: dict[str, Any],
        receipt_event: dict[str, Any],
        key_path: Path | None,
    ) -> bool:
        """Re-verify the stored Independent Verifier attestation; fail closed on any doubt.

        The audit database is writable by the Agent host, so a row that merely
        *claims* resolution proves nothing. Resolution needs the verifier's
        Ed25519 signature, re-checked against the configured public key and
        bound to this incident, request and executor receipt.
        """
        if key_path is None:
            return False
        # Local import: verifier_attestation itself depends on this module.
        from core.safety.verifier_attestation import (
            InvalidVerifierSignature,
            VerifierAttestation,
            verify_attestation_signature,
        )

        try:
            attestation = VerifierAttestation.model_validate(payload.get("verifier_attestation"))
            digest = verify_attestation_signature(attestation, key_path)
        except (InvalidVerifierSignature, ValidationError, ValueError, TypeError, OSError):
            return False
        signed = attestation.payload
        stored_verification = payload.get("verification")
        return (
            hmac.compare_digest(digest, str(payload.get("attestation_sha256", "")))
            and signed.version == 1
            and signed.incident_id == incident_id
            and signed.request_id == request_id
            and signed.scenario_id == payload.get("scenario_id")
            and signed.receipt_event_hash == receipt_event.get("event_hash")
            and signed.verification.incident_id == incident_id
            and signed.verification.verdict == "resolved"
            and signed.verification.resolution_eligible is True
            and signed.verification.simulated is False
            and stored_verification == signed.verification.model_dump(mode="json")
        )

    def get_live_incident_status(
        self,
        incident_id: str,
        request_id: str,
        *,
        verifier_public_key_path: str | Path | None = None,
    ) -> str | None:
        """Rebuild the request's live state from its verified append-only events.

        ``"resolved"`` is reported only when a stored, signed Independent
        Verifier attestation re-verifies against the verifier public key.
        Missing key, missing or invalid attestation yields ``"verifying"``.
        """
        if not self.verify_chain(incident_id):
            raise ValueError("safe-action audit chain is invalid")
        events = self.list_for_incident(incident_id)
        receipt_event = next((
            event for event in reversed(events)
            if event["event_type"] == "live_executor_result"
            and event["payload"].get("request_id") == request_id
        ), None)
        execution = receipt_event["payload"] if receipt_event else None
        if not isinstance(execution, dict) or execution.get("status") != "executed" or execution.get("mutated") is not True:
            return None

        key_path = self._resolve_verifier_key(verifier_public_key_path)
        status = "executed"
        for event in events:
            payload = event["payload"]
            if payload.get("request_id") != request_id:
                continue
            if event["event_type"] == "live_verification_pending":
                status = "verifying"
            elif event["event_type"] == "live_verification_result":
                verification = payload.get("verification")
                transition = payload.get("state_transition")
                if (
                    payload.get("status") == "resolved"
                    and isinstance(verification, dict)
                    and verification.get("incident_id") == incident_id
                    and verification.get("verdict") == "resolved"
                    and verification.get("resolution_eligible") is True
                    and verification.get("simulated") is False
                    and isinstance(transition, dict)
                    and transition.get("incident_id") == incident_id
                    and transition.get("actor") == "independent_verifier"
                    and transition.get("old_state") == "verifying"
                    and transition.get("new_state") == "resolved"
                    and self._attestation_is_authentic(
                        incident_id=incident_id,
                        request_id=request_id,
                        payload=payload,
                        receipt_event=receipt_event,
                        key_path=key_path,
                    )
                ):
                    status = "resolved"
                else:
                    status = "verifying"
        return status


class _Adapter(Protocol):
    def execute(self, request: SafeActionRequest) -> ActionResult: ...


class SafeDryRunOrchestrator:
    """Record pre/post snapshots and rollback intent; never performs a rollback."""

    def __init__(self, *, audit: SafeActionAuditStore, adapter: _Adapter) -> None:
        self._audit, self._adapter = audit, adapter

    def run(self, request: SafeActionRequest, gate: GateVerdict, *, pre_snapshot: dict[str, Any]) -> ActionResult | None:
        try:
            request = SafeActionRequest.model_validate(
                request.model_dump(mode="python", warnings=False)
            )
            gate = GateVerdict.model_validate(gate.model_dump(mode="python", warnings=False))
        except (AttributeError, TypeError, ValueError, ValidationError):
            self._audit.append(
                incident_id="unknown",
                event_type="invalid_gate_or_request",
                payload={"failure": "schema_validation"},
            )
            return None
        incident_id = request.action.incident_id
        expected_hash = SafeExecutionGate.action_hash(request)
        self._audit.append(incident_id=incident_id, event_type="gate", payload=gate.model_dump())
        if (
            gate.decision != "ALLOW"
            or not hmac.compare_digest(gate.action_sha256, expected_hash)
            or request.dry_run is not True
        ):
            self._audit.append(
                incident_id=incident_id,
                event_type="gate_rejected",
                payload={"reason": "decision_or_request_binding_failed"},
            )
            return None
        self._audit.append(
            incident_id=incident_id,
            event_type="pre_snapshot",
            payload=redact_snapshot(pre_snapshot),
        )
        try:
            raw_result = self._adapter.execute(request)
            result = ActionResult.model_validate(
                raw_result.model_dump(mode="python", warnings=False)
            )
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            self._audit.append(
                incident_id=incident_id,
                event_type="adapter_result_invalid",
                payload={"failure_type": type(exc).__name__},
            )
            return None
        if (
            result.request_id != request.request_id
            or result.idempotency_key != request.idempotency_key
            or result.executed is not False
        ):
            self._audit.append(
                incident_id=incident_id,
                event_type="adapter_result_invalid",
                payload={"failure_type": "identity_or_execution_state_mismatch"},
            )
            return None
        result = ActionResult.model_validate(
            {
                **result.model_dump(mode="python"),
                "sanitized_output": sanitize_log_excerpt(
                    sanitize_adapter_output(result.sanitized_output), max_chars=2048
                ),
            }
        )
        self._audit.append(incident_id=incident_id, event_type="adapter_result", payload=result.model_dump())
        self._audit.append(incident_id=incident_id, event_type="post_snapshot", payload={"dry_run": True, "executed": result.executed})
        if not result.success:
            self._audit.append(
                incident_id=incident_id, event_type="rollback_planned",
                payload={"dry_run": True, "reason": result.error_code, "rollback_available": request.action.rollback_plan.available},
            )
        return result
