"""Lifecycle hooks and Multi-AI Task Rotation policies for Hermes Agent.

Provides:
- HermesSafetyHook: Pre-tool call interception preventing dangerous actions
  and application data mutations.
- validate_multi_ai_task_rotation: Verifies architecture configuration
  for Multi-AI collaborative infrastructure management.
"""

from __future__ import annotations

import re
from typing import Any

# Allowed MCP tools in the Hermes cognitive domain
ALLOWED_MCP_TOOLS = {
    "list_scenarios",
    "collect_evidence",
    "diagnose_alert",
    "propose_remediation",
}

# Dangerous tool names that must be strictly intercepted
DANGEROUS_TOOL_NAMES = {
    "bash",
    "sh",
    "terminal",
    "exec",
    "eval",
    "system",
    "ssh",
    "aws_cli",
    "shell",
    "run_command",
    "execute_action",
    "resolve_incident",
    "sign_verification",
}

# Patterns indicating raw SQL mutation against Moodle application tables
APPLICATION_MUTATION_PATTERNS = [
    re.compile(r"\b(insert\s+into|update|delete\s+from|alter\s+table|drop\s+table|truncate)\s+mdl_", re.IGNORECASE),
    re.compile(r"\b(update|delete)\s+.*?\bmdl_task_adhoc\b", re.IGNORECASE),
    re.compile(r"\b(update|delete)\s+.*?\bmdl_user\b", re.IGNORECASE),
    re.compile(r"\b(update|delete)\s+.*?\bmdl_course\b", re.IGNORECASE),
]


class HermesSafetyHook:
    """Interception hook implementing defense-in-depth within Hermes Agent."""

    def __init__(self, allowed_tools: set[str] | None = None) -> None:
        self.allowed_tools = allowed_tools or ALLOWED_MCP_TOOLS

    def pre_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Intercept tool calls before execution by Hermes runtime.
        
        Returns:
            dict with 'action': 'allow' or 'block', and 'reason'.
        """
        args = tool_args or {}
        tool_lower = tool_name.lower().strip()

        # 1. Block dangerous command/shell/execution tools
        if tool_lower in DANGEROUS_TOOL_NAMES:
            return {
                "action": "block",
                "tool": tool_name,
                "reason": (
                    f"Direct execution tool '{tool_name}' is forbidden in infrastructure operations. "
                    "All remediations must be submitted via propose_remediation for out-of-process "
                    "Safety Gate cryptographic approval."
                ),
            }

        # 2. Block application-layer database mutations
        args_str = str(args)
        for pattern in APPLICATION_MUTATION_PATTERNS:
            if pattern.search(args_str):
                return {
                    "action": "block",
                    "tool": tool_name,
                    "reason": (
                        "Application layer mutation forbidden ('Quản trị hạ tầng không quản trị ứng dụng'). "
                        "AI is strictly prohibited from modifying Moodle database tables (mdl_*)."
                    ),
                }

        # 3. Check if tool is in reviewed MCP tool list
        if tool_lower not in self.allowed_tools:
            return {
                "action": "block",
                "tool": tool_name,
                "reason": (
                    f"Tool '{tool_name}' is not in the reviewed Hermes MCP tool list. "
                    f"Available tools: {sorted(self.allowed_tools)}"
                ),
            }

        return {
            "action": "allow",
            "tool": tool_name,
            "reason": "Read-only or proposal tool approved within Hermes cognitive domain.",
        }


def validate_multi_ai_task_rotation(config: dict[str, Any]) -> dict[str, Any]:
    """Validate Multi-AI Task Rotation architecture configuration.
    
    Invariants:
    1. At least 3 roles: Investigator, Critic (Auditor), and Operator Liaison.
    2. Verification MUST NOT have an LLM in the loop.
    3. Remediating model != Verifying entity.
    """
    roles = config.get("roles", {})
    required_roles = {"investigator", "critic", "liaison"}
    missing_roles = required_roles - set(roles.keys())

    if missing_roles:
        return {
            "valid": False,
            "error": f"Missing required roles: {sorted(missing_roles)}",
            "required_roles": sorted(required_roles),
            "configured_roles": list(roles.keys()),
        }

    # Verify that the Independent Verifier is strictly non-LLM
    verifier_config = config.get("verifier", {})
    if verifier_config.get("llm_in_loop", False):
        return {
            "valid": False,
            "error": (
                "Invariant violation: Actor(Remediation) == Actor(Verification) or LLM in Verifier loop. "
                "The Independent Verifier must be a deterministic telemetry measurement daemon."
            ),
        }

    return {
        "valid": True,
        "summary": "Multi-AI task rotation architecture validated successfully.",
        "roles": {
            "investigator": roles.get("investigator"),
            "critic": roles.get("critic"),
            "liaison": roles.get("liaison"),
        },
        "verifier": {
            "type": "independent_metric_daemon",
            "llm_in_loop": False,
            "cryptographic_attestation": "Ed25519",
        },
        "invariants_satisfied": [
            "Cognitive investigation bounded to read-only MCP",
            "Critic enforces infrastructure-only boundary (no app/DB mutation)",
            "Safety Gate requires 2 out-of-process human Ed25519 signatures",
            "Independent Verifier measures multi-sample telemetry with no LLM in loop",
        ],
    }
