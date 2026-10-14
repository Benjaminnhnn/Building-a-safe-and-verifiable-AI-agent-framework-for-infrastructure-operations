from __future__ import annotations

import pytest
from core.action_catalog import ActionCatalog, ActionPermission, CatalogEntry
from core.adapters import (
    SafeActionRequest, SafeAnsibleDryRunAdapter, SafeDockerDryRunAdapter,
    SafeNetworkDryRunAdapter, SafeProbeDryRunAdapter,
)
from core.safe_action_audit import SafeActionAuditStore
from core.safe_action_shadow import SafeActionShadowWorkflow
from core.safe_execution_gate import SafeExecutionGate
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment


_SCENARIOS = [
    ("DB-01", "remove_scoped_db_reject", "staging_moodle_nodes", "docker"),
    ("RES-01", "remove_named_cpu_load_container", "moodle-app-b", "docker"),
    ("NET-01", "recreate_moodle_web_from_reviewed_compose", "staging_moodle_nodes", "docker"),
    ("CON-01", "start_reviewed_compose_service", "moodle-app-b", "docker"),
    ("SEC-02", "restore_fixture_directory_mode", "moodledata_synthetic_fixture_directory", "ansible"),
]


def _workflow(tmp_path):
    audit = SafeActionAuditStore(tmp_path / "audit.db")
    rollback_catalog = ActionCatalog([
        CatalogEntry(
            action_id="restore_scoped_db_reject",
            description="Test-only inverse entry for shadow gate-flow coverage.",
            adapter="docker",
            permission=ActionPermission(required_role="executor", environment_scope=["staging"]),
            blast_radius="LOW",
            is_reversible=True,
            rollback_action="remove_scoped_db_reject",
            idempotent=True,
        ),
        CatalogEntry(
            action_id="stop_reviewed_compose_service",
            description="Test-only inverse entry for shadow gate-flow coverage.",
            adapter="docker",
            permission=ActionPermission(required_role="executor", environment_scope=["staging"]),
            blast_radius="LOW",
            is_reversible=True,
            rollback_action="start_reviewed_compose_service",
            idempotent=True,
        ),
    ])
    return audit, SafeActionShadowWorkflow(
        gate=SafeExecutionGate(
            approval_signing_key="key", approved_actors={"alice"}, rollback_catalog=rollback_catalog
        ),
        audit=audit,
        adapters={"docker": SafeDockerDryRunAdapter(), "network": SafeNetworkDryRunAdapter(),
                  "ansible": SafeAnsibleDryRunAdapter(), "probe": SafeProbeDryRunAdapter()},
    )


def _request(scenario: str, action_id: str, target: str) -> SafeActionRequest:
    action_type = {
        "remove_scoped_db_reject": ActionType.REMOVE_SCOPED_PORT_BLOCK,
        "remove_named_cpu_load_container": ActionType.STOP_FAULT_INJECTOR,
        "recreate_moodle_web_from_reviewed_compose": ActionType.RUN_ANSIBLE_PLAYBOOK,
        "start_reviewed_compose_service": ActionType.START_CONTAINER,
        "restore_fixture_directory_mode": ActionType.RESTORE_MOODLEDATA_PERMISSION,
    }.get(action_id, ActionType.READ_HEALTH)
    return SafeActionRequest(
        request_id=f"req-{scenario}", idempotency_key=f"idem-{scenario}", catalog_action_id=action_id,
        scenario_id=scenario, target_scope=target,
        action=TypedAction(
            action_id=f"act-{scenario}", incident_id=f"inc-{scenario}", action_type=action_type,
            target_resource_id=("moodledata-volume" if action_id == "restore_fixture_directory_mode" else "moodle-app"),
            environment=Environment.STAGING, reason="three evidence records",
            evidence_refs=["ev-1", "ev-2", "ev-3"], expected_outcome="recovered", reversible=True,
            rollback_plan=RollbackPlan(
                available=True,
                rollback_action_id={
                    "remove_scoped_db_reject": "restore_scoped_db_reject",
                    "start_reviewed_compose_service": "stop_reviewed_compose_service",
                }.get(action_id),
                method="Restore the prior fixture state.",
            ),
        ),
    )


@pytest.mark.parametrize("scenario,action_id,target,adapter", _SCENARIOS)
def test_shadow_workflow_replays_all_reviewed_remediations_without_mutation(tmp_path, scenario, action_id, target, adapter) -> None:
    audit, workflow = _workflow(tmp_path)
    try:
        report = workflow.run(_request(scenario, action_id, target), actor_role="executor", confidence=0.9,
                              pre_snapshot={"status": "fault_present", "token": "redact"})
        denied_for_missing_rollback = action_id in {
            "remove_named_cpu_load_container",
            "recreate_moodle_web_from_reviewed_compose",
            "restore_fixture_directory_mode",
        }
        assert report["status"] == ("denied" if denied_for_missing_rollback else "shadow_complete")
        assert report["execution_permitted"] is False and report["mutated"] is False
        if denied_for_missing_rollback:
            assert "catalogued reversible rollback plan" in report["gate"]["reason"]
        else:
            assert report["adapter"] == adapter and report["result"]["executed"] is False
        assert audit.verify_chain(f"inc-{scenario}") is True
    finally:
        audit.close()


def test_shadow_workflow_denies_invalid_request_without_adapter_execution(tmp_path) -> None:
    audit, workflow = _workflow(tmp_path)
    try:
        report = workflow.run(_request("DB-01", "unrestricted_shell", "staging_moodle_nodes"),
                              actor_role="executor", confidence=0.9, pre_snapshot={})
        assert report["status"] == "denied"
        assert report["execution_permitted"] is False
        assert [event["event_type"] for event in audit.list_for_incident("inc-DB-01")] == ["shadow_gate_rejected"]
    finally:
        audit.close()
