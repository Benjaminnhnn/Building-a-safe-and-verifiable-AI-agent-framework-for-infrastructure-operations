"""S3.1 tests: Moodle resources, scenarios and action permissions stay aligned."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.moodle_contract import (
    EXPECTED_SCENARIO_IDS,
    build_moodle_scenario_binding,
    assert_moodle_capability_contract,
    load_moodle_capability_contract,
    load_moodle_ground_truth,
    load_moodle_resource_inventory,
    validate_moodle_capability_contract,
)
from core.action_catalog import ActionCatalog


def test_s3_1_contract_is_valid_and_staging_only() -> None:
    assert_moodle_capability_contract()
    assert load_moodle_capability_contract()["environment"] == "staging"


def test_s3_1_contract_covers_exactly_fifteen_moodle_scenarios() -> None:
    contract = load_moodle_capability_contract()
    assert set(load_moodle_ground_truth()) == EXPECTED_SCENARIO_IDS
    assert set(contract["scenario_capabilities"]) == EXPECTED_SCENARIO_IDS


def test_every_declared_target_maps_to_real_inventory_resources() -> None:
    contract = load_moodle_capability_contract()
    resources = {item["resource_id"] for item in load_moodle_resource_inventory()["resources"]}
    for scope in contract["target_scopes"].values():
        assert set(scope["resource_ids"]) <= resources


def test_runtime_and_evaluation_inventory_remain_identical() -> None:
    runtime = load_moodle_resource_inventory()
    evaluation = json.loads(
        Path("evaluation/resources/moodle_resource_inventory.json").read_text(encoding="utf-8")
    )
    assert runtime == evaluation


def test_contract_rejects_a_remediation_outside_the_allowlist() -> None:
    contract = load_moodle_capability_contract()
    scenarios = load_moodle_ground_truth()
    scenarios["DB-01"]["allowed_remediation"].append(
        {"action": "blocked_unrestricted_shell", "target": "staging_moodle_nodes"}
    )
    errors = validate_moodle_capability_contract(
        contract, load_moodle_resource_inventory(), scenarios
    )
    assert any("outside the typed catalog" in error for error in errors)


def test_s3_4_scenario_map_keeps_live_boundary_at_five_ground_truth_cases() -> None:
    """Every GT remediation is typed, while only the reviewed five are live."""
    catalog = ActionCatalog.load_moodle_catalog()
    contract = load_moodle_capability_contract()
    scenarios = load_moodle_ground_truth()
    live_scenarios: set[str] = set()

    for scenario_id, scenario in scenarios.items():
        remediations = scenario["allowed_remediation"]
        assert remediations, scenario_id
        for remediation in remediations:
            action_id = remediation["action"]
            target = remediation["target"]
            assert catalog.lookup(action_id) is not None, (scenario_id, action_id)
            assert target in contract["target_scopes"]
            if catalog.is_safe_execution_allowed(
                action_id,
                scenario_id=scenario_id,
                target_scope=target,
                environment="staging",
                role="executor",
            ):
                live_scenarios.add(scenario_id)

    assert live_scenarios == {"DB-01", "RES-01", "NET-01", "CON-01", "SEC-02"}


def test_s3_4_bindings_cover_all_scenarios_and_are_fail_closed() -> None:
    bindings = {
        scenario_id: build_moodle_scenario_binding(scenario)
        for scenario_id, scenario in load_moodle_ground_truth().items()
    }
    assert set(bindings) == EXPECTED_SCENARIO_IDS
    assert sum(item["execution_lane"] == "staging_live_allowlisted" for item in bindings.values()) == 5
    assert sum(item["execution_lane"] == "offline_or_shadow_only" for item in bindings.values()) == 10
    assert sum(item["live_allowlisted_action_count"] for item in bindings.values()) == 10
    assert all(item["execution_permitted_by_binding"] is False for item in bindings.values())

    broken_scenario = load_moodle_ground_truth()["DB-01"]
    broken_scenario["allowed_remediation"][0]["target"] = "unreviewed-host"
    with pytest.raises(ValueError, match="outside target_scopes"):
        build_moodle_scenario_binding(broken_scenario)

    broken_scenario = load_moodle_ground_truth()["DB-01"]
    broken_scenario["allowed_remediation"][0]["action"] = "unrestricted_shell"
    with pytest.raises(ValueError, match="outside typed catalog"):
        build_moodle_scenario_binding(broken_scenario)
