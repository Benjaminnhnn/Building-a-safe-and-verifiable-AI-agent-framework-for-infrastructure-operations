from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import UUID

import pytest
from core.adapters import ActionResult, SafeActionRequest, SafeDockerDryRunAdapter
from core.safe_action_audit import SafeActionAuditStore, SafeDryRunOrchestrator
from core.safe_execution_gate import GateVerdict, SafeExecutionGate
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment

import core.safe_action_audit as safe_action_audit


def _request(suffix: str = "1") -> SafeActionRequest:
    return SafeActionRequest(
        request_id=f"req-{suffix}", idempotency_key=f"idem-{suffix}", catalog_action_id="remove_scoped_db_reject",
        scenario_id="DB-01", target_scope="staging_moodle_nodes",
        action=TypedAction(
            action_id=f"act-{suffix}", incident_id=f"inc-{suffix}", action_type=ActionType.REMOVE_SCOPED_PORT_BLOCK,
            target_resource_id="postgres-db", environment=Environment.STAGING, reason="evidence",
            evidence_refs=["ev-1", "ev-2", "ev-3"], expected_outcome="recovered", reversible=True,
            rollback_plan=RollbackPlan(available=True, method="Restore the prior fixture state."),
        ),
    )


def _allow(request: SafeActionRequest) -> GateVerdict:
    return GateVerdict(
        decision="ALLOW",
        reason="test",
        action_sha256=SafeExecutionGate.action_hash(request),
    )


def test_audit_is_hash_chained_append_only_and_redacted(tmp_path) -> None:
    with SafeActionAuditStore(tmp_path / "audit.db") as audit:
        audit.append(incident_id="inc-1", event_type="pre_snapshot", payload={"token": "hidden", "state": "ok"})
        audit.append(incident_id="inc-1", event_type="post_snapshot", payload={"password": "hidden", "state": "ok"})
        events = audit.list_for_incident("inc-1")
        assert audit.verify_chain("inc-1") is True
        assert events[0]["payload"]["token"] == "[REDACTED]"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            audit._connection.execute("DELETE FROM safe_action_audit")


def test_audit_redacts_api_access_keys_connection_strings_and_embedded_headers(tmp_path) -> None:
    snapshot = {
        "nested": {
            "api_key": "plain-api-key",
            "AWS_ACCESS_KEY_ID": "AKIA1234567890ABCDEF",
            "database_url": "postgresql://user:db-password@db.internal/moodle",
        },
        "request_header": "Authorization: Bearer header-token-value",
    }
    with SafeActionAuditStore(tmp_path / "audit-secrets.db") as audit:
        event = audit.append(incident_id="inc-secret", event_type="pre_snapshot", payload=snapshot)
        serialized = str(event["payload"])

    assert event["payload"]["nested"] == {
        "api_key": "[REDACTED]",
        "AWS_ACCESS_KEY_ID": "[REDACTED]",
        "database_url": "[REDACTED]",
    }
    assert "plain-api-key" not in serialized
    assert "AKIA1234567890ABCDEF" not in serialized
    assert "db-password" not in serialized
    assert "header-token-value" not in serialized


def test_audit_chain_order_uses_append_order_when_timestamps_collide(tmp_path, monkeypatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 5, tzinfo=tz or timezone.utc)

    ids = iter([UUID(int=3), UUID(int=2), UUID(int=1)])
    monkeypatch.setattr(safe_action_audit, "datetime", FixedDatetime)
    monkeypatch.setattr(safe_action_audit.uuid, "uuid4", lambda: next(ids))

    with SafeActionAuditStore(tmp_path / "audit.db") as audit:
        for event_type in ("first", "second", "third"):
            audit.append(incident_id="inc-collision", event_type=event_type, payload={})

        assert [event["event_type"] for event in audit.list_for_incident("inc-collision")] == [
            "first", "second", "third"
        ]
        assert audit.verify_chain("inc-collision") is True


def test_orchestrator_records_dry_run_snapshots(tmp_path) -> None:
    with SafeActionAuditStore(tmp_path / "audit.db") as audit:
        request = _request()
        result = SafeDryRunOrchestrator(audit=audit, adapter=SafeDockerDryRunAdapter()).run(
            request, _allow(request), pre_snapshot={"secret": "never-store", "health": "degraded"}
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
        orchestrator = SafeDryRunOrchestrator(audit=audit, adapter=_TimeoutAdapter())
        rollback_plans = 0
        for index in range(10):
            suffix = str(index)
            request = _request(suffix)
            result = orchestrator.run(
                request, _allow(request), pre_snapshot={"state": "before"}
            )
            assert result is not None and result.error_code == "timeout"
            incident_events = audit.list_for_incident(f"inc-{suffix}")
            rollback = incident_events[-1]
            assert rollback["event_type"] == "rollback_planned"
            assert rollback["payload"]["dry_run"] is True
            assert audit.verify_chain(f"inc-{suffix}") is True
            rollback_plans += 1
        assert rollback_plans == 10


def test_safe_dry_run_rejects_gate_bound_to_another_request(tmp_path) -> None:
    request = _request()
    other = _request("other")
    with SafeActionAuditStore(tmp_path / "mismatched-gate.db") as audit:
        result = SafeDryRunOrchestrator(
            audit=audit, adapter=SafeDockerDryRunAdapter()
        ).run(request, _allow(other), pre_snapshot={})

        assert result is None
        assert [event["event_type"] for event in audit.list_for_incident("inc-1")] == [
            "gate",
            "gate_rejected",
        ]


def test_safe_dry_run_rejects_mismatched_adapter_result_identity(tmp_path) -> None:
    class MismatchedAdapter:
        def execute(self, request: SafeActionRequest) -> ActionResult:
            return ActionResult(
                request_id="different-request",
                idempotency_key=request.idempotency_key,
                adapter_name="test",
                success=True,
                executed=False,
                sanitized_output="ok",
            )

    request = _request("mismatch")
    with SafeActionAuditStore(tmp_path / "mismatched-adapter.db") as audit:
        result = SafeDryRunOrchestrator(audit=audit, adapter=MismatchedAdapter()).run(
            request, _allow(request), pre_snapshot={}
        )

        assert result is None
        events = audit.list_for_incident(request.action.incident_id)
        assert events[-1]["event_type"] == "adapter_result_invalid"
