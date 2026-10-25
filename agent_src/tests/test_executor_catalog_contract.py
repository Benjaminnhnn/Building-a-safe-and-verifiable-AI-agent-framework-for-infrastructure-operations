"""The agent catalog, executor API allowlist and host forced command must agree.

An action the agent may propose but the executor or host refuses would pass
two approvals and then fail at dispatch; this keeps the three lists aligned.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from core.safety.action_catalog import (
    _BIZ_STAGING_EXECUTION_SCOPES,
    _S3_3_STAGING_EXECUTION_SCOPES,
    ActionCatalog,
)

REPO = Path(__file__).resolve().parents[2]
API = REPO / "automation" / "moodle-safe-executor-api.py"
APPROVAL = REPO / "automation" / "moodle-safe-approval.py"
PLAYBOOK = REPO / "ansible" / "playbooks" / "configure-moodle-safe-executor.yml"


def _literal_catalog(path: Path):
    # The API reads required environment variables at import, so parse it.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "CATALOG" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"CATALOG not found in {path.name}")


def _api_catalog() -> dict[tuple[str, str, str], tuple[str, ...]]:
    return _literal_catalog(API)


def _approval_catalog() -> set[tuple[str, str, str]]:
    return {
        (scenario_id, action_id, scope)
        for scenario_id, actions in _literal_catalog(APPROVAL).items()
        for action_id, scope in actions.items()
    }


def _playbook_script(dest: str) -> str:
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    for play in plays:
        for task in play.get("tasks", []):
            copy = task.get("ansible.builtin.copy") or {}
            if copy.get("dest") == dest:
                return copy["content"]
    raise AssertionError(f"{dest} not found in {PLAYBOOK.name}")


def _forced_command_map() -> dict[str, str]:
    script = _playbook_script("/usr/local/sbin/moodle-safe-executor-force")
    mapping: dict[str, str] = {}
    for patterns, scenario in re.findall(r"^\s*([A-Za-z0-9_|-]+)\)\s*\n\s*scenario=([A-Z0-9-]+)", script, re.MULTILINE):
        for pattern in patterns.split("|"):
            mapping[pattern] = scenario
    return mapping


def _host_actions() -> set[str]:
    script = _playbook_script("/usr/local/sbin/moodle-safe-executor")
    return set(re.findall(r"^\s*([A-Z]{2,3}-0[0-9](?:-ROLLBACK)?)\)", script, re.MULTILINE))


def _mutation_scopes() -> set[tuple[str, str, str]]:
    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    return {
        (scenario_id, action_id, scope)
        for scopes in (_S3_3_STAGING_EXECUTION_SCOPES, _BIZ_STAGING_EXECUTION_SCOPES)
        for scenario_id, actions in scopes.items()
        for action_id, scope in actions.items()
        if not catalog.lookup(action_id).permission.read_only
    }


@pytest.mark.parametrize("entry", sorted(_mutation_scopes()))
def test_every_agent_mutation_is_in_the_executor_allowlist(entry: tuple[str, str, str]) -> None:
    assert entry in _api_catalog()


def test_executor_allowlist_has_no_action_the_agent_cannot_propose() -> None:
    assert set(_api_catalog()) == _mutation_scopes()


def test_operator_approval_tool_signs_exactly_the_executor_allowlist() -> None:
    assert _approval_catalog() == set(_api_catalog())


@pytest.mark.parametrize("entry", sorted(_mutation_scopes()))
def test_every_allowlisted_action_has_a_forced_command_and_host_branch(entry: tuple[str, str, str]) -> None:
    _, action_id, _ = entry
    forced = _forced_command_map()
    assert action_id in forced, f"{action_id} has no forced-command mapping"
    assert forced[action_id] in _host_actions(), f"{forced[action_id]} has no host executor branch"


def test_biz01_rollback_restores_maintenance_instead_of_repeating_the_disable() -> None:
    forced = _forced_command_map()
    assert forced["disable_moodle_cli_maintenance_mode"] == "BIZ-01"
    assert forced["enable_moodle_cli_maintenance_mode"] == "BIZ-01-ROLLBACK"


def test_biz01_host_branch_refuses_during_an_upgrade() -> None:
    script = _playbook_script("/usr/local/sbin/moodle-safe-executor")
    branch = script.split("BIZ-01)", 1)[1].split(";;", 1)[0]
    assert "admin/cli/upgrade.php" in branch
    assert "exit 75" in branch
    assert branch.index("upgrade.php") < branch.index("--disable")


def test_sec01_is_never_executable() -> None:
    assert not any(scenario == "SEC-01" for scenario, _, _ in _api_catalog())
    assert "SEC-01" not in _forced_command_map().values()
