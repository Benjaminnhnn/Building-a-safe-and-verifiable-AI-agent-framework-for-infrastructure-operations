"""End-to-end shadow workflow for the bounded S3.3 Moodle action slice."""

from __future__ import annotations

from typing import Any

from core.action_catalog import ActionCatalog
from core.adapters import SafeActionRequest, SafeDryRunAdapter
from core.safe_action_audit import SafeActionAuditStore, SafeDryRunOrchestrator
from core.safe_execution_gate import ApprovalRecord, SafeExecutionGate


class SafeActionShadowWorkflow:
    """Connect catalog → gate → dry-run adapter → audit without live authority."""

    def __init__(
        self,
        *,
        gate: SafeExecutionGate,
        audit: SafeActionAuditStore,
        adapters: dict[str, SafeDryRunAdapter],
        catalog: ActionCatalog | None = None,
    ) -> None:
        self._gate = gate
        self._audit = audit
        self._adapters = adapters
        self._catalog = catalog or ActionCatalog.load_moodle_s3_3_execution_catalog()

    def run(
        self,
        request: SafeActionRequest,
        *,
        actor_role: str,
        confidence: float,
        pre_snapshot: dict[str, Any],
        approval: ApprovalRecord | None = None,
    ) -> dict[str, Any]:
        verdict = self._gate.evaluate(
            request, actor_role=actor_role, confidence=confidence, approval=approval
        )
        entry = self._catalog.lookup(request.catalog_action_id)
        if verdict.decision != "ALLOW" or entry is None:
            self._audit.append(
                incident_id=request.action.incident_id,
                event_type="shadow_gate_rejected",
                payload=verdict.model_dump(),
            )
            return {
                "status": "denied" if verdict.decision == "DENY" else "awaiting_approval",
                "execution_permitted": False,
                "mutated": False,
                "gate": verdict.model_dump(),
            }

        adapter = self._adapters.get(entry.adapter)
        if adapter is None:
            self._audit.append(
                incident_id=request.action.incident_id,
                event_type="shadow_adapter_missing",
                payload={"adapter": entry.adapter},
            )
            return {
                "status": "escalated",
                "execution_permitted": False,
                "mutated": False,
                "gate": verdict.model_dump(),
            }

        result = SafeDryRunOrchestrator(audit=self._audit, adapter=adapter).run(
            request, verdict, pre_snapshot=pre_snapshot
        )
        return {
            "status": "shadow_complete" if result and result.success else "escalated",
            "execution_permitted": False,
            "mutated": False,
            "gate": verdict.model_dump(),
            "adapter": entry.adapter,
            "result": result.model_dump() if result else None,
        }
