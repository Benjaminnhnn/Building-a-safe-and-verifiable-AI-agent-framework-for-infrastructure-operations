"""Model Context Protocol (MCP) Server for Hermes Agent Integration.

Enforces strict separation of concerns for infrastructure operations:
- Cognitive Domain (Hermes): Read-only telemetry, root-cause diagnosis,
  and reversible remediation proposals.
- External Safety Domain (Safety Gate): Out-of-process multi-party Ed25519
  cryptographic approvals.
- Verification Domain (Independent Verifier): Metric/telemetry multi-sample
  attestation before any incident transitions to RESOLVED.

"Quản trị hạ tầng không quản trị ứng dụng":
AI cannot mutate Moodle application data, users, plugins, or database rows.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from core.evidence.evidence_collectors import (
    BUSINESS_METRIC_QUERIES_BY_ALERT,
    METRIC_QUERIES_BY_ALERT,
)
from core.moodle.diagnosis import diagnose_moodle_alert
from core.safety.action_catalog import MOODLE_7_SCENARIO_IDS

# Metadata for the 7 reviewed scenarios (2 Business/Infra + 5 Technical)
SCENARIO_METADATA: dict[str, dict[str, Any]] = {
    "BIZ-01": {
        "scenario_id": "BIZ-01",
        "name": "Moodle Maintenance Mode Stuck after Update",
        "alert_name": "MoodleMaintenanceModeStuck",
        "layer": "infrastructure",
        "description": "Student access blocked by maintenance flag left behind after upgrade finished.",
        "disposition": "REVIEWED_STAGING_EXECUTION",
        "allowed_action": "disable_moodle_cli_maintenance_mode",
        "human_escalation_only": False,
        "target_scope": "staging_moodle_app",
        "safety_principle": "Quản trị hạ tầng không quản trị ứng dụng; disable maintenance via CLI only if upgrade=0.0",
    },
    "BIZ-02": {
        "scenario_id": "BIZ-02",
        "name": "Moodle Ad-hoc / Cron Task Queue Overdue",
        "alert_name": "MoodleAdhocTaskQueueStuck",
        "layer": "infrastructure",
        "description": "Background tasks overdue because moodle-cron container stopped on node A.",
        "disposition": "REVIEWED_STAGING_EXECUTION",
        "allowed_action": "restart_moodle_cron_service",
        "human_escalation_only": False,
        "target_scope": "staging_moodle_app",
        "safety_principle": "Restart moodle-cron container at infra level; never mutate mdl_task_adhoc table in DB",
    },
    "CON-01": {
        "scenario_id": "CON-01",
        "name": "Moodle Web Container Stopped",
        "alert_name": "MoodleWebContainerMissing",
        "layer": "infrastructure",
        "description": "Moodle web container stopped on node B; ALB target degraded.",
        "disposition": "REVIEWED_STAGING_EXECUTION",
        "allowed_action": "start_reviewed_compose_service",
        "human_escalation_only": False,
        "target_scope": "moodle-app-b",
        "safety_principle": "Start reviewed Compose service on node B; do not modify compose file",
    },
    "DB-01": {
        "scenario_id": "DB-01",
        "name": "RDS Database Connectivity Lost",
        "alert_name": "MoodleNodeRdsTcpFailed",
        "layer": "infrastructure",
        "description": "TCP path drop from app nodes to RDS PostgreSQL due to scoped reject rule.",
        "disposition": "REVIEWED_STAGING_EXECUTION",
        "allowed_action": "remove_scoped_db_reject",
        "human_escalation_only": False,
        "target_scope": "staging_moodle_nodes",
        "safety_principle": "Remove only tagged incident iptables rule; do not alter RDS parameters",
    },
    "RES-01": {
        "scenario_id": "RES-01",
        "name": "Node CPU Saturation",
        "alert_name": "MoodleNodeCpuHigh",
        "layer": "infrastructure",
        "description": "App node B CPU saturated by known stress container.",
        "disposition": "REVIEWED_STAGING_EXECUTION",
        "allowed_action": "remove_named_cpu_load_container",
        "human_escalation_only": False,
        "target_scope": "moodle-app-b",
        "safety_principle": "Stop only known test stress container; never kill core OS or DB processes",
    },
    "NET-01": {
        "scenario_id": "NET-01",
        "name": "Network / Dependency Resolution Drift",
        "alert_name": "MoodleSyntheticTransactionFailed",
        "layer": "infrastructure",
        "description": "Internal name resolution drift inside Moodle web container.",
        "disposition": "REVIEWED_STAGING_EXECUTION",
        "allowed_action": "recreate_moodle_web_from_reviewed_compose",
        "human_escalation_only": False,
        "target_scope": "staging_moodle_nodes",
        "safety_principle": "Recreate container from immutable reviewed compose; do not edit DNS / hosts directly",
    },
    "SEC-01": {
        "scenario_id": "SEC-01",
        "name": "Database Security Group Ingress Drift",
        "alert_name": "MoodleDatabasePortExposure",
        "layer": "infrastructure",
        "description": "Unauthorized security group ingress rule exposing port 5432 to 0.0.0.0/0.",
        "disposition": "REVIEWED_HUMAN_ONLY",
        "allowed_action": None,
        "human_escalation_only": True,
        "target_scope": "staging_rds_postgresql",
        "safety_principle": "HUMAN_ONLY. AI proposes investigation and escalates to Security Lead; automated mutation forbidden",
    },
}

# The strictly bounded set of MCP tool definitions exposed to Hermes
TOOL_DEFINITIONS = [
    {
        "name": "list_scenarios",
        "description": "List the 7 reviewed infrastructure operational scenarios supported by the AI ops framework.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "collect_evidence",
        "description": "Collect read-only telemetry and metric queries for a given scenario. Strictly non-mutating.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario_id": {
                    "type": "string",
                    "description": "The scenario ID (e.g. BIZ-01, BIZ-02, CON-01, DB-01, RES-01, NET-01, SEC-01)",
                },
                "telemetry_data": {
                    "type": "object",
                    "description": "Optional telemetry readings observed from Prometheus / Blackbox / Docker",
                },
            },
            "required": ["scenario_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "diagnose_alert",
        "description": "Perform root-cause diagnosis based on collected evidence indicators.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario_id": {
                    "type": "string",
                    "description": "The scenario ID being diagnosed",
                },
                "alert_name": {
                    "type": "string",
                    "description": "The active Prometheus alert name",
                },
                "telemetry_data": {
                    "type": "object",
                    "description": "Dictionary of metric query results and status flags",
                },
            },
            "required": ["scenario_id", "alert_name", "telemetry_data"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_remediation",
        "description": "Propose a bounded, reversible remediation plan for human review. Does NOT execute.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario_id": {
                    "type": "string",
                    "description": "The scenario ID to propose remediation for",
                },
                "diagnosis_result": {
                    "type": "object",
                    "description": "The diagnosis object from diagnose_alert",
                },
            },
            "required": ["scenario_id", "diagnosis_result"],
            "additionalProperties": False,
        },
    },
]


class HermesMcpServer:
    """Read-only Model Context Protocol (MCP) server for Hermes Agent."""

    def __init__(self) -> None:
        self.scenarios = SCENARIO_METADATA

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return schema definitions of available tools.
        
        Strict invariant: Only read-only and proposal tools are exposed.
        No execution or verification tools exist.
        """
        return TOOL_DEFINITIONS

    def list_scenarios(self) -> dict[str, Any]:
        """List the 7 reviewed infrastructure operational scenarios."""
        return {
            "total_scenarios": len(self.scenarios),
            "scope": "infrastructure_only",
            "principle": "Quản trị hạ tầng không quản trị ứng dụng",
            "scenarios": [
                {
                    "scenario_id": meta["scenario_id"],
                    "name": meta["name"],
                    "alert_name": meta["alert_name"],
                    "layer": meta["layer"],
                    "disposition": meta["disposition"],
                    "human_escalation_only": meta["human_escalation_only"],
                }
                for meta in self.scenarios.values()
            ],
        }

    def collect_evidence(
        self,
        scenario_id: str,
        telemetry_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Collect read-only telemetry queries and indicators for a scenario."""
        sid = scenario_id.upper().strip()
        if sid not in self.scenarios:
            return {
                "status": "error",
                "error": f"Scenario '{scenario_id}' is not in the reviewed 7-scenario catalog.",
                "valid_scenarios": list(self.scenarios.keys()),
            }

        meta = self.scenarios[sid]
        alert_name = meta["alert_name"]

        # Look up metric queries from collectors
        metric_tuples = METRIC_QUERIES_BY_ALERT.get(
            alert_name,
            BUSINESS_METRIC_QUERIES_BY_ALERT.get(alert_name, ()),
        )
        if metric_tuples:
            required_queries = [item[0] for item in metric_tuples]
        else:
            required_queries = ["security_group_ingress_rule"]

        telemetry = telemetry_data or {}

        return {
            "status": "success",
            "scenario_id": sid,
            "alert_name": alert_name,
            "target_scope": meta["target_scope"],
            "mode": "read_only",
            "required_queries": required_queries,
            "telemetry_observed": telemetry,
            "read_only_guarantee": True,
        }

    def diagnose_alert(
        self,
        scenario_id: str,
        alert_name: str,
        telemetry_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Perform evidence-derived diagnosis."""
        sid = scenario_id.upper().strip()
        if sid not in self.scenarios:
            return {
                "status": "error",
                "error": f"Scenario '{scenario_id}' is not in the reviewed 7-scenario catalog.",
            }

        meta = self.scenarios[sid]

        # 1. BIZ-01: Stuck maintenance mode after update
        if sid == "BIZ-01":
            upgrade_running = float(telemetry_data.get("moodle_upgrade_in_progress", 0.0))
            maintenance_on = float(telemetry_data.get("moodle_cli_maintenance_mode_enabled", 0.0))

            if upgrade_running >= 1.0:
                return {
                    "scenario_id": sid,
                    "status": "escalated_human_only",
                    "cause_code": "upgrade_in_progress_human_escalation",
                    "human_escalation_only": True,
                    "reason": "Active Moodle upgrade process is running. Automated action is strictly prohibited.",
                    "evidence_refs": ["moodle_upgrade_in_progress=1.0"],
                }
            if maintenance_on >= 1.0:
                return {
                    "scenario_id": sid,
                    "status": "diagnosed",
                    "cause_code": "maintenance_mode_stuck_after_update",
                    "human_escalation_only": False,
                    "reason": "CLI maintenance mode flag remains active after upgrade completed successfully.",
                    "evidence_refs": [
                        "moodle_cli_maintenance_mode_enabled=1.0",
                        "moodle_upgrade_in_progress=0.0",
                    ],
                }
            return {
                "scenario_id": sid,
                "status": "inconclusive",
                "reason": "Maintenance mode indicator is not active.",
            }

        # 2. BIZ-02: Cron / Ad-hoc task queue stuck
        if sid == "BIZ-02":
            cron_stopped = telemetry_data.get("moodle_cron_container_stopped", {})
            if isinstance(cron_stopped, dict):
                node_a_stopped = float(cron_stopped.get("moodle-app-a", 0.0))
            else:
                node_a_stopped = float(cron_stopped)

            overdue_count = float(telemetry_data.get("moodle_adhoc_tasks_overdue_count", 0.0))

            if node_a_stopped >= 1.0 and overdue_count > 0:
                return {
                    "scenario_id": sid,
                    "status": "diagnosed",
                    "cause_code": "moodle_cron_service_stopped",
                    "human_escalation_only": False,
                    "reason": "moodle-cron container on node A is stopped, causing ad-hoc tasks to backlog.",
                    "evidence_refs": [
                        "moodle_cron_container_stopped[node-a]=1.0",
                        f"moodle_adhoc_tasks_overdue_count={overdue_count}",
                    ],
                }
            return {
                "scenario_id": sid,
                "status": "inconclusive",
                "reason": "moodle-cron container is running or no overdue tasks found.",
            }

        # 3. SEC-01: Database SG ingress rule
        if sid == "SEC-01":
            return {
                "scenario_id": sid,
                "status": "diagnosed",
                "cause_code": "unauthorized_database_ingress_security_group_rule",
                "human_escalation_only": True,
                "reason": "Database security group has unauthorized public ingress rule. Human escalation only.",
                "evidence_refs": ["ingress_rule:0.0.0.0/0->5432"],
            }

        # 4. Standard technical scenarios via diagnose_moodle_alert
        diagnosis = diagnose_moodle_alert(
            alert={"labels": {"alertname": alert_name}},
            metric_telemetry=telemetry_data,
        )
        return {
            "scenario_id": sid,
            "status": "diagnosed" if diagnosis.get("status") == "diagnosed" else "inconclusive",
            "cause_code": diagnosis.get("cause_code"),
            "human_escalation_only": False,
            "reason": diagnosis.get("summary", ""),
            "evidence_refs": diagnosis.get("evidence_refs", []),
        }

    def propose_remediation(
        self,
        scenario_id: str,
        diagnosis_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Propose a safe remediation plan for human review.
        
        Strict invariant:
        - Fails closed for SEC-01 (HUMAN_ONLY).
        - Fails closed if upgrade is active for BIZ-01.
        - Fails closed for unreviewed scenarios.
        - Result is strictly PENDING_SAFETY_GATE_APPROVAL, never executed.
        """
        sid = scenario_id.upper().strip()
        if sid not in self.scenarios:
            return {
                "status": "refused",
                "error": f"Scenario '{scenario_id}' is not in the reviewed 7-scenario catalog.",
            }

        meta = self.scenarios[sid]

        # Invariant 1: SEC-01 is strictly HUMAN_ONLY
        if meta["human_escalation_only"] or sid == "SEC-01":
            return {
                "status": "refused",
                "scenario_id": sid,
                "disposition": "REVIEWED_HUMAN_ONLY",
                "human_escalation_only": True,
                "error": "Scenario SEC-01 is classified as HUMAN_ONLY. Automated remediation is strictly forbidden by policy.",
                "escalation_target": "security_operations_team",
            }

        # Invariant 2: Diagnosis must be valid
        if diagnosis_result.get("human_escalation_only") or diagnosis_result.get("status") == "escalated_human_only":
            return {
                "status": "refused",
                "scenario_id": sid,
                "disposition": "ESCALATED_HUMAN_ONLY",
                "error": f"Diagnosis requires human escalation: {diagnosis_result.get('reason')}",
            }

        if diagnosis_result.get("status") != "diagnosed":
            return {
                "status": "refused",
                "scenario_id": sid,
                "error": "Cannot propose remediation without a conclusive diagnosed root cause.",
            }

        # Build proposal with cryptographic safety requirements
        proposal_id = f"prop-{sid.lower()}-{uuid.uuid4().hex[:8]}"
        action_type = meta["allowed_action"]

        rollback_plans = {
            "disable_moodle_cli_maintenance_mode": "Enable CLI maintenance mode if application behaves erratically.",
            "restart_moodle_cron_service": "Inspect docker container logs and restart moodle-cron on node B if node A fails.",
            "start_reviewed_compose_service": "Stop the service container if unrecoverable runtime errors appear.",
            "remove_scoped_db_reject": "Re-apply iptables reject rule if traffic originates from an untrusted source.",
            "remove_named_cpu_load_container": "Re-launch load container in controlled test harness if needed.",
            "recreate_moodle_web_from_reviewed_compose": "Revert to previous container revision if container health fails.",
        }

        return {
            "status": "proposal_generated",
            "proposal_id": proposal_id,
            "scenario_id": sid,
            "action_type": action_type,
            "target_resource": "moodle-app" if sid.startswith("BIZ") else meta.get("target_resource", "moodle-app"),
            "target_scope": meta["target_scope"],
            "disposition": "REVIEWED_STAGING_EXECUTION",
            "safety_gate_required": True,
            "required_approvals": ["operator", "safety_lead"],
            "cryptographic_signatures_required": 2,
            "signature_algorithm": "Ed25519",
            "reversible": True,
            "rollback_plan": rollback_plans.get(str(action_type), "Default rollback via verified staging snapshot."),
            "state": "PENDING_SAFETY_GATE_APPROVAL",
            "note": "Quản trị hạ tầng không quản trị ứng dụng: hành động can thiệp ở tầng container/dịch vụ/mạng.",
        }

    def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tool calls. Fails closed on any unknown or mutation tool."""
        if tool_name == "list_scenarios":
            return self.list_scenarios()
        if tool_name == "collect_evidence":
            return self.collect_evidence(
                scenario_id=arguments.get("scenario_id", ""),
                telemetry_data=arguments.get("telemetry_data"),
            )
        if tool_name == "diagnose_alert":
            return self.diagnose_alert(
                scenario_id=arguments.get("scenario_id", ""),
                alert_name=arguments.get("alert_name", ""),
                telemetry_data=arguments.get("telemetry_data", {}),
            )
        if tool_name == "propose_remediation":
            return self.propose_remediation(
                scenario_id=arguments.get("scenario_id", ""),
                diagnosis_result=arguments.get("diagnosis_result", {}),
            )

        # Invariant: Any attempt to execute or verify directly is rejected
        return {
            "error": f"Tool '{tool_name}' is not supported in Hermes cognitive domain.",
            "reason": "Direct execution and verification are strictly forbidden in MCP. Mutations require out-of-process Safety Gate approval.",
        }

    def handle_jsonrpc(self, request_data: str | dict[str, Any]) -> dict[str, Any]:
        """Process standard JSON-RPC 2.0 requests from MCP clients (Hermes)."""
        if isinstance(request_data, str):
            try:
                req = json.loads(request_data)
            except json.JSONDecodeError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
        else:
            req = request_data

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "moodle-hermes-mcp-server",
                        "version": "1.0.0",
                    },
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.get_tool_schemas()},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            result = self.execute_tool(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2),
                        }
                    ]
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method '{method}' not found or unsupported.",
            },
        }


def main() -> None:
    """Run stdio loop for Hermes MCP client."""
    server = HermesMcpServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = server.handle_jsonrpc(line)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
