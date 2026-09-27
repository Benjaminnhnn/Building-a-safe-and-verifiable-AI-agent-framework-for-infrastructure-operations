"""S3.1 tests: Moodle resources, scenarios and action permissions stay aligned."""

from __future__ import annotations

import json
from pathlib import Path

from core.moodle_contract import (
    EXPECTED_SCENARIO_IDS,
    assert_moodle_capability_contract,
    load_moodle_capability_contract,
    load_moodle_ground_truth,
    load_moodle_resource_inventory,
    validate_moodle_capability_contract,
)


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
