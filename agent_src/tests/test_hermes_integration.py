"""Test suite for Hermes Agent integration, MCP server, and safety hooks.

Verifies:
1. MCP server strictly exposes read-only telemetry and proposal tools (no execution or verification tools).
2. The 7 reviewed infrastructure operational scenarios are exposed accurately.
3. Root-cause diagnosis enforces the "Quản trị hạ tầng không quản trị ứng dụng" boundary:
   - BIZ-01: Escalates to human if an upgrade is active; proposes CLI disable only when completed.
   - BIZ-02: Proposes restarting moodle-cron container at infra level; never mutates DB tables.
   - SEC-01: Strictly classified as HUMAN_ONLY; automated remediation is refused.
4. JSON-RPC 2.0 protocol compliance for Hermes MCP client connectivity.
5. HermesSafetyHook intercepts and blocks shell execution and Moodle application table mutations.
6. Multi-AI task rotation architecture enforces non-LLM independent verification.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from integrations.hermes.hooks import (
    ALLOWED_MCP_TOOLS,
    DANGEROUS_TOOL_NAMES,
    HermesSafetyHook,
    validate_multi_ai_task_rotation,
)
from integrations.hermes.mcp_server import (
    SCENARIO_METADATA,
    TOOL_DEFINITIONS,
    HermesMcpServer,
)


@pytest.fixture
def mcp_server() -> HermesMcpServer:
    return HermesMcpServer()


@pytest.fixture
def safety_hook() -> HermesSafetyHook:
    return HermesSafetyHook()


def test_hermes_mcp_server_tool_schemas_strictly_bounded(mcp_server: HermesMcpServer) -> None:
    """Tool schema must expose ONLY read-only and proposal tools."""
    schemas = mcp_server.get_tool_schemas()
    tool_names = {tool["name"] for tool in schemas}

    assert tool_names == {"list_scenarios", "collect_evidence", "diagnose_alert", "propose_remediation"}

    # Invariant: No execution, mutation, or resolution tools in MCP schema
    for forbidden in ["execute_action", "run_command", "resolve_incident", "sign_attestation", "approve_action"]:
        assert forbidden not in tool_names


def test_hermes_mcp_list_scenarios_returns_7_reviewed_scenarios(mcp_server: HermesMcpServer) -> None:
    """Server must return exactly the 7 reviewed infrastructure scenarios."""
    res = mcp_server.list_scenarios()
    assert res["total_scenarios"] == 7
    assert res["principle"] == "Quản trị hạ tầng không quản trị ứng dụng"

    scenarios = {s["scenario_id"]: s for s in res["scenarios"]}
    expected_sids = {"BIZ-01", "BIZ-02", "CON-01", "DB-01", "RES-01", "NET-01", "SEC-01"}
    assert set(scenarios.keys()) == expected_sids

    # SEC-01 must be human escalation only
    assert scenarios["SEC-01"]["human_escalation_only"] is True
    assert scenarios["SEC-01"]["disposition"] == "REVIEWED_HUMAN_ONLY"

    # Other 6 scenarios are automated remediation candidates
    for sid in ["BIZ-01", "BIZ-02", "CON-01", "DB-01", "RES-01", "NET-01"]:
        assert scenarios[sid]["human_escalation_only"] is False


def test_hermes_mcp_collect_evidence_read_only(mcp_server: HermesMcpServer) -> None:
    """Collecting evidence returns metric queries and maintains read-only guarantee."""
    for sid in ["BIZ-01", "BIZ-02", "CON-01", "DB-01", "RES-01", "NET-01", "SEC-01"]:
        res = mcp_server.collect_evidence(scenario_id=sid)
        assert res["status"] == "success"
        assert res["scenario_id"] == sid
        assert res["read_only_guarantee"] is True
        assert len(res["required_queries"]) > 0

    # Unknown scenario fails closed
    err_res = mcp_server.collect_evidence(scenario_id="UNKNOWN-99")
    assert err_res["status"] == "error"
    assert "not in the reviewed 7-scenario catalog" in err_res["error"]


def test_hermes_mcp_diagnose_biz01_maintenance_stuck_vs_upgrade_running(mcp_server: HermesMcpServer) -> None:
    """BIZ-01: Proposes CLI disable if upgrade=0.0, but escalates to human if upgrade>=1.0."""
    # Case A: Upgrade completed (0.0), maintenance flag stuck (1.0) -> Diagnosed
    telemetry_completed = {
        "moodle_upgrade_in_progress": 0.0,
        "moodle_cli_maintenance_mode_enabled": 1.0,
    }
    diag_a = mcp_server.diagnose_alert(
        scenario_id="BIZ-01",
        alert_name="MoodleMaintenanceModeStuck",
        telemetry_data=telemetry_completed,
    )
    assert diag_a["status"] == "diagnosed"
    assert diag_a["cause_code"] == "maintenance_mode_stuck_after_update"
    assert diag_a["human_escalation_only"] is False

    # Case B: Upgrade active (1.0) -> Escalated to human only
    telemetry_upgrading = {
        "moodle_upgrade_in_progress": 1.0,
        "moodle_cli_maintenance_mode_enabled": 1.0,
    }
    diag_b = mcp_server.diagnose_alert(
        scenario_id="BIZ-01",
        alert_name="MoodleMaintenanceModeStuck",
        telemetry_data=telemetry_upgrading,
    )
    assert diag_b["status"] == "escalated_human_only"
    assert diag_b["cause_code"] == "upgrade_in_progress_human_escalation"
    assert diag_b["human_escalation_only"] is True


def test_hermes_mcp_diagnose_biz02_cron_container_stopped(mcp_server: HermesMcpServer) -> None:
    """BIZ-02: Diagnoses cron service stopped on node A causing adhoc task backlog."""
    telemetry = {
        "moodle_cron_container_stopped": {"moodle-app-a": 1.0, "moodle-app-b": 0.0},
        "moodle_adhoc_tasks_overdue_count": 25.0,
    }
    diag = mcp_server.diagnose_alert(
        scenario_id="BIZ-02",
        alert_name="MoodleAdhocTaskQueueStuck",
        telemetry_data=telemetry,
    )
    assert diag["status"] == "diagnosed"
    assert diag["cause_code"] == "moodle_cron_service_stopped"
    assert diag["human_escalation_only"] is False


def test_hermes_mcp_diagnose_sec01_human_escalation_only(mcp_server: HermesMcpServer) -> None:
    """SEC-01: Must diagnose unauthorized ingress rule and flag human escalation."""
    diag = mcp_server.diagnose_alert(
        scenario_id="SEC-01",
        alert_name="MoodleDatabasePublicAccessDetected",
        telemetry_data={},
    )
    assert diag["status"] == "diagnosed"
    assert diag["cause_code"] == "unauthorized_database_ingress_security_group_rule"
    assert diag["human_escalation_only"] is True


def test_hermes_mcp_propose_remediation_allows_valid_scenarios(mcp_server: HermesMcpServer) -> None:
    """Proposals for BIZ-01, BIZ-02, CON-01, DB-01, RES-01, NET-01 require 2 Ed25519 signatures."""
    # BIZ-01
    prop_biz01 = mcp_server.propose_remediation(
        scenario_id="BIZ-01",
        diagnosis_result={"status": "diagnosed", "cause_code": "maintenance_mode_stuck_after_update"},
    )
    assert prop_biz01["status"] == "proposal_generated"
    assert prop_biz01["action_type"] == "disable_moodle_cli_maintenance_mode"
    assert prop_biz01["state"] == "PENDING_SAFETY_GATE_APPROVAL"
    assert prop_biz01["cryptographic_signatures_required"] == 2
    assert prop_biz01["signature_algorithm"] == "Ed25519"

    # BIZ-02 (Infrastructure container restart, not application SQL mutation)
    prop_biz02 = mcp_server.propose_remediation(
        scenario_id="BIZ-02",
        diagnosis_result={"status": "diagnosed", "cause_code": "moodle_cron_service_stopped"},
    )
    assert prop_biz02["status"] == "proposal_generated"
    assert prop_biz02["action_type"] == "restart_moodle_cron_service"
    assert prop_biz02["state"] == "PENDING_SAFETY_GATE_APPROVAL"

    # Technical scenarios
    for sid, expected_action in [
        ("CON-01", "start_reviewed_compose_service"),
        ("DB-01", "remove_scoped_db_reject"),
        ("RES-01", "remove_named_cpu_load_container"),
        ("NET-01", "recreate_moodle_web_from_reviewed_compose"),
    ]:
        prop = mcp_server.propose_remediation(
            scenario_id=sid,
            diagnosis_result={"status": "diagnosed", "cause_code": "test_cause"},
        )
        assert prop["status"] == "proposal_generated"
        assert prop["action_type"] == expected_action
        assert prop["state"] == "PENDING_SAFETY_GATE_APPROVAL"


def test_hermes_mcp_propose_remediation_fails_closed_for_sec01(mcp_server: HermesMcpServer) -> None:
    """SEC-01 remediation proposal must be refused by policy."""
    prop = mcp_server.propose_remediation(
        scenario_id="SEC-01",
        diagnosis_result={"status": "diagnosed", "human_escalation_only": True},
    )
    assert prop["status"] == "refused"
    assert prop["human_escalation_only"] is True
    assert "HUMAN_ONLY" in prop["error"]
    assert "automated remediation is strictly forbidden" in prop["error"].lower()


def test_hermes_mcp_propose_remediation_fails_closed_when_biz01_upgrade_active(
    mcp_server: HermesMcpServer,
) -> None:
    """BIZ-01 remediation must be refused if upgrade process is active."""
    prop = mcp_server.propose_remediation(
        scenario_id="BIZ-01",
        diagnosis_result={
            "status": "escalated_human_only",
            "human_escalation_only": True,
            "reason": "Active Moodle upgrade process is running.",
        },
    )
    assert prop["status"] == "refused"
    assert "requires human escalation" in prop["error"]


def test_hermes_mcp_propose_unreviewed_scenario_refused(mcp_server: HermesMcpServer) -> None:
    """Unreviewed scenarios (e.g. BIZ-03, BIZ-04) must fail closed."""
    prop = mcp_server.propose_remediation(
        scenario_id="BIZ-03",
        diagnosis_result={"status": "diagnosed"},
    )
    assert prop["status"] == "refused"
    assert "not in the reviewed 7-scenario catalog" in prop["error"]


def test_hermes_mcp_execute_unsupported_tool_refused(mcp_server: HermesMcpServer) -> None:
    """Executing direct actions or resolutions is rejected by the MCP server."""
    res_exec = mcp_server.execute_tool("execute_action", {"action": "restart"})
    assert "not supported in Hermes cognitive domain" in res_exec["error"]

    res_resolve = mcp_server.execute_tool("resolve_incident", {"incident_id": "INC-123"})
    assert "not supported in Hermes cognitive domain" in res_resolve["error"]


def test_hermes_mcp_jsonrpc_protocol_compliance(mcp_server: HermesMcpServer) -> None:
    """Test full JSON-RPC 2.0 protocol compliance for MCP integration."""
    # 1. initialize
    init_req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    init_resp = mcp_server.handle_jsonrpc(init_req)
    assert init_resp["id"] == 1
    assert init_resp["result"]["serverInfo"]["name"] == "moodle-hermes-mcp-server"

    # 2. tools/list
    list_req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    list_resp = mcp_server.handle_jsonrpc(list_req)
    assert list_resp["id"] == 2
    tools = list_resp["result"]["tools"]
    assert len(tools) == 4

    # 3. tools/call: list_scenarios
    call_req = json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "list_scenarios", "arguments": {}},
    })
    call_resp = mcp_server.handle_jsonrpc(call_req)
    assert call_resp["id"] == 3
    content_text = call_resp["result"]["content"][0]["text"]
    parsed_content = json.loads(content_text)
    assert parsed_content["total_scenarios"] == 7

    # 4. Unknown method returns -32601
    bad_req = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/non_existent"})
    bad_resp = mcp_server.handle_jsonrpc(bad_req)
    assert bad_resp["error"]["code"] == -32601


def test_hermes_safety_hook_blocks_dangerous_tools(safety_hook: HermesSafetyHook) -> None:
    """Safety hook blocks bash, sh, exec, terminal, and direct mutation tools."""
    for dangerous in DANGEROUS_TOOL_NAMES:
        res = safety_hook.pre_tool_call(dangerous, {"command": "echo test"})
        assert res["action"] == "block"
        assert "forbidden" in res["reason"]


def test_hermes_safety_hook_blocks_app_layer_db_mutations(safety_hook: HermesSafetyHook) -> None:
    """Safety hook blocks SQL queries mutating Moodle application tables."""
    bad_calls = [
        ("query_db", {"sql": "UPDATE mdl_user SET confirmed=1 WHERE id=5"}),
        ("query_db", {"sql": "DELETE FROM mdl_task_adhoc WHERE id=12"}),
        ("query_db", {"sql": "INSERT INTO mdl_course (fullname) VALUES ('AI Course')"}),
        ("query_db", {"sql": "DROP TABLE mdl_config"}),
    ]
    for tool, args in bad_calls:
        res = safety_hook.pre_tool_call(tool, args)
        assert res["action"] == "block"
        assert "Application layer mutation forbidden" in res["reason"]


def test_hermes_safety_hook_allows_safe_tools(safety_hook: HermesSafetyHook) -> None:
    """Safety hook allows reviewed read-only and proposal tools."""
    for tool in ALLOWED_MCP_TOOLS:
        res = safety_hook.pre_tool_call(tool, {})
        assert res["action"] == "allow"


def test_multi_ai_task_rotation_validation() -> None:
    """Multi-AI task rotation must include Investigator, Critic, and Liaison with non-LLM verifier."""
    # Valid configuration
    valid_cfg = {
        "roles": {
            "investigator": {"model": "hermes-3-70b", "scope": "read_only_mcp"},
            "critic": {"model": "claude-3-5-sonnet", "scope": "boundary_audit"},
            "liaison": {"model": "gpt-4o", "scope": "telegram_operator"},
        },
        "verifier": {
            "type": "independent_metric_daemon",
            "llm_in_loop": False,
        },
    }
    res = validate_multi_ai_task_rotation(valid_cfg)
    assert res["valid"] is True
    assert len(res["invariants_satisfied"]) == 4

    # Invalid: missing role
    bad_cfg_missing = {
        "roles": {
            "investigator": {"model": "hermes-3-70b"},
        },
        "verifier": {"llm_in_loop": False},
    }
    res_bad = validate_multi_ai_task_rotation(bad_cfg_missing)
    assert res_bad["valid"] is False
    assert "Missing required roles" in res_bad["error"]

    # Invalid: LLM in verifier loop violates verification invariant
    bad_cfg_verifier = {
        "roles": {
            "investigator": {"model": "hermes-3-70b"},
            "critic": {"model": "claude-3-5-sonnet"},
            "liaison": {"model": "gpt-4o"},
        },
        "verifier": {
            "type": "llm_agent",
            "llm_in_loop": True,
        },
    }
    res_bad_v = validate_multi_ai_task_rotation(bad_cfg_verifier)
    assert res_bad_v["valid"] is False
    assert "Actor(Remediation) == Actor(Verification)" in res_bad_v["error"]
