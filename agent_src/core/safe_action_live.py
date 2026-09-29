"""Explicit, action-bound live execution path for the S3.3 staging slice.

This workflow is not called from the alert handler. A caller must provide an
independent Ed25519 approval, pass the Safety Gate, and receive an executed
result from the separately kill-switched forced-command API. It never marks an
incident resolved; that remains the job of a later independent verifier.
"""

from __future__ import annotations

from typing import Any, Callable

from core.action_catalog import ActionCatalog
from core.adapters import SafeLiveActionRequest
from core.safe_action_audit import SafeActionAuditStore
from core.safe_execution_gate import ApprovalRecord, GateVerdict, SafeExecutionGate
from core.safe_executor_client import action_digest, execute_approved_staging_action

_Executor = Callable[..., dict[str, Any]]


class SafeActionLiveWorkflow:
    """Gate → append-only audit → signed executor request; never auto-resolve."""

    def __init__(
        self,
        *,
        gate: SafeExecutionGate,
        audit: SafeActionAuditStore,
        catalog: ActionCatalog | None = None,
        executor: _Executor = execute_approved_staging_action,
    ) -> None:
        self._gate = gate
        self._audit = audit
        self._catalog = catalog or ActionCatalog.load_moodle_s3_3_execution_catalog()
        self._executor = executor

    def run(
        self,
        request: SafeLiveActionRequest,
        *,
        actor_role: str,
        confidence: float,
        pre_snapshot: dict[str, Any],
        gate_approval: ApprovalRecord | None,
        executor_approval: dict[str, str] | None,
    ) -> dict[str, Any]:
        incident_id = request.action.incident_id
        verdict = self._gate.evaluate(
            request,
            actor_role=actor_role,
            confidence=confidence,
            approval=gate_approval,
        )
        self._audit.append(
            incident_id=incident_id,
            event_type="live_gate",
            payload={**verdict.model_dump(), "request_id": request.request_id},
        )
        if verdict.decision != "ALLOW":
            return self._not_executed(
                "awaiting_approval" if verdict.decision == "REQUIRE_APPROVAL" else "denied",
                verdict,
                verdict.reason,
            )

        catalog_entry = self._catalog.lookup(request.catalog_action_id)
        if catalog_entry is None or catalog_entry.permission.read_only:
            return self._not_executed("denied", verdict, "catalog action is missing or read-only")

        expected_api_digest = action_digest(
            request.scenario_id,
            request.catalog_action_id,
            request.target_scope,
            request.idempotency_key,
        )
        if (
            not isinstance(executor_approval, dict)
            or set(executor_approval) != {"actor_id", "expires_at", "action_sha256", "signature"}
            or executor_approval.get("actor_id") not in {"operator", "human-approver"}
            or executor_approval.get("action_sha256") != expected_api_digest
            or not all(isinstance(executor_approval.get(key), str) for key in ("expires_at", "signature"))
        ):
            self._audit.append(
                incident_id=incident_id,
                event_type="live_executor_approval_rejected",
                payload={"reason": "missing or action-mismatched operator signature"},
            )
            return self._not_executed("awaiting_approval", verdict, "valid API approval is required")

        self._audit.append(
            incident_id=incident_id,
            event_type="live_pre_snapshot",
            payload=pre_snapshot,
        )
        try:
            result = self._executor(
                scenario_id=request.scenario_id,
                catalog_action_id=request.catalog_action_id,
                target_scope=request.target_scope,
                idempotency_key=request.idempotency_key,
                approval=executor_approval,
            )
        except Exception as exc:  # fail closed; exception text may contain transport details
            safe_result = {"status": "transport_error", "error_type": type(exc).__name__, "mutated": False}
            self._audit.append(incident_id=incident_id, event_type="live_executor_result", payload=safe_result)
            return {
                "status": "escalated",
                "execution_permitted": False,
                "mutated": False,
                "resolution_eligible": False,
                "gate": verdict.model_dump(),
                "result": safe_result,
            }

        safe_result = {
            "status": result.get("status"),
            "scenario_id": result.get("scenario_id"),
            "completed_nodes": result.get("completed_nodes", []),
            "failure": result.get("failure"),
            "mutated": bool(result.get("mutated", False)),
        }
        self._audit.append(incident_id=incident_id, event_type="live_executor_result", payload=safe_result)
        executed = safe_result["status"] == "executed"
        self._audit.append(
            incident_id=incident_id,
            event_type="live_verification_pending",
            payload={"verifier_status": "not_run", "resolution_eligible": False},
        )
        return {
            "status": "awaiting_verification" if executed else "escalated",
            "execution_permitted": executed,
            "mutated": safe_result["mutated"],
            "resolution_eligible": False,
            "gate": verdict.model_dump(),
            "adapter": "moodle_safe_executor_api",
            "result": safe_result,
        }

    @staticmethod
    def _not_executed(status: str, verdict: GateVerdict, reason: str) -> dict[str, Any]:
        return {
            "status": status,
            "execution_permitted": False,
            "mutated": False,
            "resolution_eligible": False,
            "reason": reason,
            "gate": verdict.model_dump(),
        }
