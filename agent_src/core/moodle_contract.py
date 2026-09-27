"""Validate the non-mutating S3.1 Moodle capability contract.

The contract joins four independently versioned inputs: the logical resource
inventory, the fifteen Moodle ground-truth scenarios, the typed action catalog,
and the evidence boundary.  It is deliberately a validation layer only; it
does not execute an adapter or make an AWS call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.action_catalog import ActionCatalog

EXPECTED_SCENARIO_IDS = frozenset(
    {
        "DB-01", "DB-02", "DB-03",
        "RES-01", "RES-02", "RES-03",
        "NET-01", "NET-02", "NET-03",
        "CON-01", "CON-02", "CON-03",
        "SEC-01", "SEC-02", "SEC-03",
    }
)
REQUIRED_EVIDENCE_FIELDS = frozenset(
    {
        "evidence_id", "incident_id", "resource_id", "source", "collected_at",
        "summary", "content_hash", "redacted",
    }
)
FORBIDDEN_ACTION_IDS = frozenset({"blocked_unrestricted_shell"})


class MoodleContractError(ValueError):
    """Raised when a checked-in Moodle contract violates its safety boundary."""


def _agent_root() -> Path:
    """Return the packaged ``agent_src`` root as well as the checkout root."""
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    """Locate a repository checkout when offline scenario fixtures are present."""
    for candidate in (Path.cwd(), *_agent_root().parents):
        if (candidate / "evaluation" / "ground_truth" / "moodle").is_dir():
            return candidate
    # A deployed Agent intentionally has no evaluation fixtures. Returning the
    # current directory gives callers a clear missing-fixture error if they try
    # to run an offline replay inside the runtime image.
    return Path.cwd()


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise MoodleContractError(f"expected JSON object: {path}")
    return payload


def load_moodle_capability_contract(path: Path | None = None) -> dict[str, Any]:
    return _load_json(path or _agent_root() / "config" / "moodle_capability_contract.json")


def load_moodle_resource_inventory(path: Path | None = None) -> dict[str, Any]:
    return _load_json(path or _agent_root() / "config" / "moodle_resource_inventory.json")


def load_moodle_ground_truth(root: Path | None = None) -> dict[str, dict[str, Any]]:
    scenario_root = root or _repo_root() / "evaluation" / "ground_truth" / "moodle"
    scenarios: dict[str, dict[str, Any]] = {}
    for path in sorted(scenario_root.glob("*.json")):
        scenario = _load_json(path)
        scenario_id = scenario.get("scenario_id")
        if isinstance(scenario_id, str):
            scenarios[scenario_id] = scenario
    return scenarios


def validate_moodle_capability_contract(
    contract: dict[str, Any],
    inventory: dict[str, Any],
    scenarios: dict[str, dict[str, Any]],
    catalog: ActionCatalog | None = None,
) -> list[str]:
    """Return all contract errors rather than failing at the first mismatch."""
    errors: list[str] = []
    if contract.get("schema_version") != "1.0":
        errors.append("contract schema_version must be 1.0")
    if contract.get("environment") != "staging":
        errors.append("contract is staging-only")

    resource_ids = {
        item.get("resource_id")
        for item in inventory.get("resources", [])
        if isinstance(item, dict) and isinstance(item.get("resource_id"), str)
    }
    if not resource_ids:
        errors.append("resource inventory is empty")

    evidence = contract.get("evidence_contract")
    if not isinstance(evidence, dict):
        errors.append("evidence_contract is missing")
    else:
        fields = set(evidence.get("required_fields", []))
        missing = sorted(REQUIRED_EVIDENCE_FIELDS - fields)
        if missing:
            errors.append(f"evidence_contract missing required fields: {missing}")
        sources = evidence.get("allowed_sources", [])
        if not isinstance(sources, list) or not sources:
            errors.append("evidence_contract must define allowed_sources")
        forbidden_metadata = set(evidence.get("prohibited_metadata_keys", []))
        if not {"password", "secret", "token", "private_key"}.issubset(forbidden_metadata):
            errors.append("evidence_contract must prohibit credential metadata")

    capabilities = contract.get("capabilities", [])
    if not isinstance(capabilities, list):
        errors.append("capabilities must be a list")
        capabilities = []
    capability_ids: set[str] = set()
    for item in capabilities:
        if not isinstance(item, dict) or not isinstance(item.get("capability_id"), str):
            errors.append("capability is missing capability_id")
            continue
        capability_id = item["capability_id"]
        if capability_id in capability_ids:
            errors.append(f"duplicate capability_id: {capability_id}")
        capability_ids.add(capability_id)
        if item.get("access") not in {"read", "scoped_write"}:
            errors.append(f"capability {capability_id} has invalid access")
        unknown = sorted(set(item.get("resource_ids", [])) - resource_ids)
        if unknown:
            errors.append(f"capability {capability_id} references unknown resources: {unknown}")

    scopes = contract.get("target_scopes", {})
    if not isinstance(scopes, dict):
        errors.append("target_scopes must be an object")
        scopes = {}
    for scope, definition in scopes.items():
        if not isinstance(definition, dict):
            errors.append(f"target scope {scope} must be an object")
            continue
        scoped_resources = definition.get("resource_ids", [])
        if not scoped_resources:
            errors.append(f"target scope {scope} has no resources")
        unknown = sorted(set(scoped_resources) - resource_ids)
        if unknown:
            errors.append(f"target scope {scope} references unknown resources: {unknown}")

    if set(scenarios) != EXPECTED_SCENARIO_IDS:
        errors.append(
            "ground truth must contain exactly the 15 Moodle scenarios; got "
            + ", ".join(sorted(scenarios))
        )
    scenario_capabilities = contract.get("scenario_capabilities", {})
    if not isinstance(scenario_capabilities, dict):
        errors.append("scenario_capabilities must be an object")
        scenario_capabilities = {}
    if set(scenario_capabilities) != EXPECTED_SCENARIO_IDS:
        errors.append("scenario_capabilities must map exactly the 15 Moodle scenarios")

    action_catalog = catalog or ActionCatalog.load_moodle_catalog()
    for scenario_id, scenario in scenarios.items():
        required = scenario_capabilities.get(scenario_id, [])
        if not required:
            errors.append(f"scenario {scenario_id} has no declared capabilities")
        unknown_capabilities = sorted(set(required) - capability_ids)
        if unknown_capabilities:
            errors.append(f"scenario {scenario_id} has unknown capabilities: {unknown_capabilities}")
        for remediation in scenario.get("allowed_remediation", []):
            if not isinstance(remediation, dict):
                errors.append(f"scenario {scenario_id} has malformed remediation")
                continue
            action_id = remediation.get("action")
            target = remediation.get("target")
            if not isinstance(action_id, str) or not action_id:
                errors.append(f"scenario {scenario_id} remediation action is missing")
            elif action_id in FORBIDDEN_ACTION_IDS or action_catalog.lookup(action_id) is None:
                errors.append(f"scenario {scenario_id} action is outside the typed catalog: {action_id}")
            if not isinstance(target, str) or target not in scopes:
                errors.append(f"scenario {scenario_id} target is outside target_scopes: {target}")
        for forbidden_action in scenario.get("forbidden_actions", []):
            if action_catalog.lookup(str(forbidden_action)) is not None:
                errors.append(f"scenario {scenario_id} forbidden action is present in typed catalog: {forbidden_action}")
    return errors


def assert_moodle_capability_contract() -> None:
    errors = validate_moodle_capability_contract(
        load_moodle_capability_contract(),
        load_moodle_resource_inventory(),
        load_moodle_ground_truth(),
    )
    if errors:
        raise MoodleContractError("; ".join(errors))
