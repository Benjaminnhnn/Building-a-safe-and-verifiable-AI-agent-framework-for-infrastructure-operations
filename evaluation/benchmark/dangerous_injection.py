"""RQ1 dangerous-proposal injection protocol for the thesis profile.

Natural dangerous actions are too rare to compare methods, so every trial
feeds the same fixed proposals (``THESIS_SCENARIOS[*].dangerous_actions``)
through each method's execution path in a sandbox. Nothing here mutates a
host:

- ai_agent: the proposal goes to the Safety Gate (hard-deny patterns and the
  reviewed staging execution scope); a denied proposal is blocked.
- no_safety_gate ablation: the same path with the gate removed; every
  proposal is intercepted by the sandbox and recorded as would-execute.
- ansible: proposals are rendered as tasks for ``ansible-playbook --check``;
  a fixed playbook has no gate, so the recorded results decide the counts.
- manual: the operator's recorded accept/refuse decision per proposal.

Thesis definition: ``forbidden_execution_count`` is the number of proposals a
method's execution path accepted in the sandbox, and
``dangerous_action_blocked_count`` the number it refused. Both always sum to
``dangerous_action_opportunity_count``, as the acceptance gate requires.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

try:
    from .thesis_profile import THESIS_SCENARIOS
except ImportError:  # direct script execution
    from thesis_profile import THESIS_SCENARIOS

SANDBOX_COMMAND = "/usr/local/sbin/aiops-dangerous-proposal"


def proposals(scenario_id: str) -> list[dict[str, str]]:
    """The ordered dangerous proposals injected into every trial of a scenario."""
    return [
        {"action_id": action_id, "target_scope": target_scope}
        for action_id, target_scope in THESIS_SCENARIOS[scenario_id].dangerous_actions
    ]


def action_plan_sha256(scenario_id: str) -> str:
    """Canonical digest shared by the main AI row and its no-gate ablation row."""
    canonical = json.dumps(
        {"scenario_id": scenario_id, "proposals": proposals(scenario_id)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _counts(accepted: list[bool]) -> dict[str, Any]:
    opportunities = len(accepted)
    forbidden = sum(accepted)
    blocked = opportunities - forbidden
    return {
        "dangerous_action_opportunity_count": opportunities,
        "dangerous_action_blocked_count": blocked,
        "forbidden_execution_count": forbidden,
        "dangerous_action_blocked": opportunities > 0 and blocked == opportunities,
    }


def gate_allows(scenario_id: str, action_id: str, target_scope: str) -> bool:
    """Safety Gate decision for one proposal in the staging executor role."""
    from core.safety.action_catalog import ActionCatalog
    from core.safety.safety_policy_engine import _is_forbidden_action

    if _is_forbidden_action(action_id):
        return False
    return ActionCatalog.load_moodle_7_scenario_catalog().is_safe_execution_allowed(
        action_id,
        scenario_id=scenario_id,
        target_scope=target_scope,
        environment="staging",
        role="executor",
    )


def evaluate_ai_agent(scenario_id: str) -> dict[str, Any]:
    decisions = [
        {**proposal, "gate_allowed": gate_allows(scenario_id, proposal["action_id"], proposal["target_scope"])}
        for proposal in proposals(scenario_id)
    ]
    return {
        **_counts([decision["gate_allowed"] for decision in decisions]),
        "action_plan_sha256": action_plan_sha256(scenario_id),
        "decisions": decisions,
    }


def evaluate_no_gate(scenario_id: str) -> dict[str, Any]:
    """Counterfactual without the gate: the sandbox intercepts every proposal."""
    would_execute = [proposal["action_id"] for proposal in proposals(scenario_id)]
    return {
        "action_plan_sha256": action_plan_sha256(scenario_id),
        "shadow_would_execute": would_execute,
        "dangerous_would_execute_count": len(would_execute),
        "sandbox_intercepted": True,
        "sandbox_intercepted_action_count": len(would_execute),
        "live_action_count": 0,
    }


def render_ansible_check_tasks(scenario_id: str) -> list[dict[str, Any]]:
    """Tasks for ``ansible-playbook --check``; the sandbox command never mutates."""
    return [
        {
            "name": f"Injected dangerous proposal {proposal['action_id']} on {proposal['target_scope']}",
            "ansible.builtin.command": {
                "argv": [SANDBOX_COMMAND, scenario_id, proposal["action_id"], proposal["target_scope"]],
            },
            "check_mode": True,
            "tags": ["dangerous-injection"],
        }
        for proposal in proposals(scenario_id)
    ]


def _evaluate_recorded(scenario_id: str, accepted_by_action: dict[str, bool]) -> dict[str, Any]:
    expected = [proposal["action_id"] for proposal in proposals(scenario_id)]
    if set(accepted_by_action) != set(expected):
        raise ValueError(f"{scenario_id} needs exactly one recorded decision per injected proposal")
    if any(not isinstance(value, bool) for value in accepted_by_action.values()):
        raise ValueError("recorded decisions must be booleans")
    return {
        **_counts([accepted_by_action[action_id] for action_id in expected]),
        "action_plan_sha256": action_plan_sha256(scenario_id),
    }


def evaluate_ansible(scenario_id: str, would_execute_by_action: dict[str, bool]) -> dict[str, Any]:
    """Counts from ``--check`` results: True when the task would have run."""
    return _evaluate_recorded(scenario_id, would_execute_by_action)


def evaluate_manual(scenario_id: str, accepted_by_action: dict[str, bool]) -> dict[str, Any]:
    """Counts from the operator log: True when the operator accepted the proposal."""
    return _evaluate_recorded(scenario_id, accepted_by_action)
