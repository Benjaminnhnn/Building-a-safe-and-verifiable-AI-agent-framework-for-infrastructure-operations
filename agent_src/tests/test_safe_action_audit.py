from __future__ import annotations

import sqlite3

import pytest
from core.adapters import ActionResult, SafeActionRequest, SafeDockerDryRunAdapter
from core.safe_action_audit import SafeActionAuditStore, SafeDryRunOrchestrator
from core.safe_execution_gate import GateVerdict
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment


def _request() -> SafeActionRequest:
    return SafeActionRequest(
        request_id="req-1", idempotency_key="idem-1", catalog_action_id="remove_scoped_db_reject",
        scenario_id="DB-01", target_scope="staging_moodle_nodes",
        action=TypedAction(
            action_id="act-1", incident_id="inc-1", action_type=ActionType.REMOVE_SCOPED_PORT_BLOCK,
            target_resource_id="postgres-db", environment=Environment.STAGING, reason="evidence",
            evidence_refs=["ev-1", "ev-2", "ev-3"], expected_outcome="recovered", reversible=True,
            rollback_plan=RollbackPlan(available=True),
        ),
    )


def _allow() -> GateVerdict:
    return GateVerdict(decision="ALLOW", reason="test", action_sha256="a" * 64)


def test_audit_is_hash_chained_append_only_and_redacted(tmp_path) -> None:
    with SafeActionAuditStore(tmp_path / "audit.db") as audit:
        audit.append(incident_id="inc-1", event_type="pre_snapshot", payload={"token": "hidden", "state": "ok"})
        audit.append(incident_id="inc-1", event_type="post_snapshot", payload={"password": "hidden", "state": "ok"})
        events = audit.list_for_incident("inc-1")
        assert audit.verify_chain("inc-1") is True
        assert events[0]["payload"]["token"] == "[REDACTED]"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            audit._connection.execute("DELETE FROM safe_action_audit")


def test_orchestrator_records_dry_run_snapshots(tmp_path) -> None:
    with SafeActionAuditStore(tmp_path / "audit.db") as audit:
        result = SafeDryRunOrchestrator(audit=audit, adapter=SafeDockerDryRunAdapter()).run(
            _request(), _allow(), pre_snapshot={"secret": "never-store", "health": "degraded"}
        )
        assert result is not None and result.success and result.executed is False
        assert [event["event_type"] for event in audit.list_for_incident("inc-1")] == [
            "gate", "pre_snapshot", "adapter_result", "post_snapshot"
        ]


class _TimeoutAdapter:
    def execute(self, request: SafeActionRequest) -> ActionResult:
        return ActionResult(request_id=request.request_id, idempotency_key=request.idempotency_key,
                            adapter_name="test", success=False, executed=False,
                            sanitized_output="timeout", error_code="timeout")


def test_timeout_or_partial_failure_only_plans_dry_run_rollback(tmp_path) -> None:
    with SafeActionAuditStore(tmp_path / "audit.db") as audit:
        result = SafeDryRunOrchestrator(audit=audit, adapter=_TimeoutAdapter()).run(
            _request(), _allow(), pre_snapshot={"state": "before"}
        )
        assert result is not None and result.error_code == "timeout"
        rollback = audit.list_for_incident("inc-1")[-1]
        assert rollback["event_type"] == "rollback_planned"
        assert rollback["payload"]["dry_run"] is True
