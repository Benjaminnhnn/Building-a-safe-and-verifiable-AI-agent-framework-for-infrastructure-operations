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
from core.sanitization import sanitize_log_excerpt

_Executor = Callable[..., dict[str, Any]]
_EXECUTOR_NODES_BY_SCOPE = {
    "staging_moodle_nodes": {"a", "b"},
    "moodle-app-b": {"b"},
    "moodledata_synthetic_fixture_directory": {"a"},
}


def _normalize_executor_result(
    result: Any, request: SafeLiveActionRequest, request_binding: dict[str, Any]
) -> dict[str, Any]:
    """Validate the authenticated executor protocol without coercing fields."""
    if not isinstance(result, dict) or type(result.get("status")) is not str:
        raise ValueError("executor response must be an object with a string status")
    status = result["status"]
    if status in {"denied", "awaiting_approval", "disabled", "in_progress"}:
        nodes = result.get("completed_nodes", [])
        if (
            result.get("mutated", False) is not False
            or nodes not in (None, [])
            or result.get("scenario_id") not in (None, request.scenario_id)
        ):
            raise ValueError("non-execution response has inconsistent mutation fields")
        reason = result.get("reason")
        return {
            "status": status,
            "scenario_id": request.scenario_id,
            "completed_nodes": [],
            "failure": sanitize_log_excerpt(reason, max_chars=240) if isinstance(reason, str) else None,
            "mutated": False,
            **request_binding,
        }

    if status not in {"executed", "partial_failure", "failed"}:
        raise ValueError("executor response has an unknown status")
    nodes = result.get("completed_nodes")
    mutated = result.get("mutated")
    failure = result.get("failure")
    allowed_nodes = _EXECUTOR_NODES_BY_SCOPE.get(request.target_scope, set())
    if (
        result.get("scenario_id") != request.scenario_id
        or not isinstance(nodes, list)
        or any(type(node) is not str or node not in allowed_nodes for node in nodes)
        or len(nodes) != len(set(nodes))
        or type(mutated) is not bool
        or mutated != bool(nodes)
    ):
        raise ValueError("executor response does not match its requested scope or mutation state")
    if failure is not None:
        if (
            not isinstance(failure, dict)
            or set(failure) - {"node", "status", "exit_code"}
            or failure.get("node") not in allowed_nodes
            or failure.get("status") not in {"failed", "timeout"}
            or ("exit_code" in failure and type(failure["exit_code"]) is not int)
        ):
            raise ValueError("executor failure details are malformed")
        failure = {
            "node": failure["node"],
            "status": failure["status"],
            **({"exit_code": failure["exit_code"]} if "exit_code" in failure else {}),
        }
    if status == "partial_failure" and failure is not None and failure["node"] in nodes:
        raise ValueError("executor marks the same node both completed and failed")
    if (
        (status == "executed" and (not mutated or failure is not None))
        or (status == "partial_failure" and (not mutated or not nodes or failure is None))
        or (status == "failed" and (mutated or nodes or failure is None))
    ):
        raise ValueError("executor status conflicts with its completion and failure fields")
    return {
        "status": status,
        "scenario_id": request.scenario_id,
        "completed_nodes": nodes,
        "failure": failure,
        "mutated": mutated,
        **request_binding,
    }


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
            self._gate.release_before_execution(request, verdict)
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
            self._gate.release_before_execution(request, verdict)
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
        request_binding = {
            "request_id": request.request_id,
            "action_id": request.action.action_id,
            "catalog_action_id": request.catalog_action_id,
            "scenario_id": request.scenario_id,
            "target_scope": request.target_scope,
            "target_resource_id": request.action.target_resource_id,
            "idempotency_key": request.idempotency_key,
            "gate_action_sha256": verdict.action_sha256,
        }
        try:
            result = self._executor(
                scenario_id=request.scenario_id,
                catalog_action_id=request.catalog_action_id,
                target_scope=request.target_scope,
                idempotency_key=request.idempotency_key,
                approval=executor_approval,
            )
        except Exception as exc:  # fail closed; exception text may contain transport details
            safe_result = {
                "status": "transport_error",
                "error_type": type(exc).__name__,
                "mutated": False,
                **request_binding,
            }
            self._audit.append(incident_id=incident_id, event_type="live_executor_result", payload=safe_result)
            return {
                "status": "escalated",
                "execution_permitted": False,
                "mutated": False,
                "resolution_eligible": False,
                "gate": verdict.model_dump(),
                "result": safe_result,
            }

        try:
            safe_result = _normalize_executor_result(result, request, request_binding)
        except (TypeError, ValueError) as exc:
            safe_result = {
                "status": "invalid_response",
                "error_type": type(exc).__name__,
                "mutated": None,
                **request_binding,
            }
            self._audit.append(
                incident_id=incident_id,
                event_type="live_executor_result",
                payload=safe_result,
            )
            self._audit.append(
                incident_id=incident_id,
                event_type="live_verification_pending",
                payload={"verifier_status": "untrusted_executor_receipt", "resolution_eligible": False},
            )
            return {
                "status": "escalated",
                "execution_permitted": True,
                "mutated": None,
                "mutation_state": "unknown",
                "resolution_eligible": False,
                "gate": verdict.model_dump(),
                "result": safe_result,
            }
        # These two API responses are emitted before its durable idempotency
        # claim. Generic `denied` can also mean replay/conflict after dispatch.
        if safe_result["status"] in {"awaiting_approval", "disabled"} and not safe_result["mutated"]:
            self._gate.release_before_execution(request, verdict)
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

    def rollback_after_failure(
        self,
        original_request: SafeLiveActionRequest,
        rollback_request: SafeLiveActionRequest,
        *,
        actor_role: str,
        confidence: float,
        pre_snapshot: dict[str, Any],
        gate_approval: ApprovalRecord | None,
        executor_approval: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Run an inverse only after the executor's audited partial-mutation receipt."""
        incident_id = original_request.action.incident_id
        if not self._audit.verify_chain(incident_id):
            return self._rollback_denied(incident_id, "execution audit chain is invalid")

        receipt = next(
            (
                event["payload"]
                for event in reversed(self._audit.list_for_incident(incident_id))
                if event["event_type"] == "live_executor_result"
                and event["payload"].get("request_id") == original_request.request_id
                and event["payload"].get("action_id") == original_request.action.action_id
                and event["payload"].get("catalog_action_id") == original_request.catalog_action_id
            ),
            None,
        )
        if receipt is None:
            return self._rollback_denied(incident_id, "original execution receipt is missing")
        expected_binding = {
            "scenario_id": original_request.scenario_id,
            "target_scope": original_request.target_scope,
            "target_resource_id": original_request.action.target_resource_id,
            "idempotency_key": original_request.idempotency_key,
            "gate_action_sha256": SafeExecutionGate.action_hash(original_request),
        }
        if any(receipt.get(key) != value for key, value in expected_binding.items()):
            return self._rollback_denied(incident_id, "original execution receipt does not bind this request")

        status = receipt.get("status")
        mutated = receipt.get("mutated") is True
        completed_nodes = receipt.get("completed_nodes")
        if status == "executed":
            return self._rollback_not_required(
                incident_id,
                "rollback after a completed action requires a persisted Independent Verifier verdict",
                "awaiting_verification",
            )
        should_rollback = (
            status == "partial_failure"
            and mutated
            and isinstance(completed_nodes, list)
            and bool(completed_nodes)
        )
        if not should_rollback:
            if not mutated and status in {"failed", "disabled", "awaiting_approval"}:
                return self._rollback_not_required(
                    incident_id, "executor receipt confirms no mutation", "not_required"
                )
            return self._rollback_denied(
                incident_id,
                "executor receipt does not prove a rollback-triggering partial mutation",
            )

        original_action = original_request.action
        rollback_action = rollback_request.action
        original_entry = self._catalog.lookup(original_request.catalog_action_id)
        rollback_entry = self._catalog.lookup(rollback_request.catalog_action_id)
        if (
            original_entry is None
            or rollback_entry is None
            or not original_action.reversible
            or not original_action.rollback_plan.available
            or original_action.rollback_plan.rollback_action_id != rollback_request.catalog_action_id
            or original_entry.rollback_action != rollback_request.catalog_action_id
            or rollback_entry.rollback_action != original_request.catalog_action_id
            or original_entry.adapter != rollback_entry.adapter
            or original_entry.permission.required_role != rollback_entry.permission.required_role
            or "staging" not in rollback_entry.permission.environment_scope
            or rollback_entry.permission.read_only
            or not rollback_entry.is_reversible
            or rollback_action.rollback_plan.rollback_action_id != original_request.catalog_action_id
            or rollback_request.request_id == original_request.request_id
            or rollback_request.idempotency_key == original_request.idempotency_key
            or rollback_request.scenario_id != original_request.scenario_id
            or rollback_request.target_scope != original_request.target_scope
            or rollback_action.incident_id != incident_id
            or rollback_action.target_resource_id != original_action.target_resource_id
            or rollback_action.environment != original_action.environment
        ):
            return self._rollback_denied(
                incident_id,
                "rollback catalog pair is unavailable or the request is not a reciprocal same-target action",
            )

        self._audit.append(
            incident_id=incident_id,
            event_type="conditional_rollback_requested",
            payload={
                "original_request_id": original_request.request_id,
                "original_action_id": original_action.action_id,
                "rollback_request_id": rollback_request.request_id,
                "rollback_action_id": rollback_action.action_id,
                "trigger": "partial_failure",
            },
        )
        result = self.run(
            rollback_request,
            actor_role=actor_role,
            confidence=confidence,
            pre_snapshot=pre_snapshot,
            gate_approval=gate_approval,
            executor_approval=executor_approval,
        )
        self._audit.append(
            incident_id=incident_id,
            event_type="conditional_rollback_result",
            payload={
                "original_request_id": original_request.request_id,
                "rollback_request_id": rollback_request.request_id,
                "status": result["status"],
                "mutated": result["mutated"],
                "resolution_eligible": False,
            },
        )
        return result

    def _rollback_denied(self, incident_id: str, reason: str) -> dict[str, Any]:
        self._audit.append(
            incident_id=incident_id,
            event_type="conditional_rollback_denied",
            payload={"reason": reason},
        )
        return {
            "status": "denied",
            "execution_permitted": False,
            "mutated": False,
            "resolution_eligible": False,
            "reason": reason,
        }

    def _rollback_not_required(
        self, incident_id: str, reason: str, status: str
    ) -> dict[str, Any]:
        self._audit.append(
            incident_id=incident_id,
            event_type="conditional_rollback_not_required",
            payload={"reason": reason, "status": status},
        )
        return {
            "status": status,
            "execution_permitted": False,
            "mutated": False,
            "resolution_eligible": False,
            "reason": reason,
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
