"""The seven thesis ground truths describe the deployed ALB/RDS/EFS system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.safety.action_catalog import ActionCatalog
from core.moodle.contract import (
    MoodleContractError,
    load_moodle_capability_contract,
    load_moodle_ground_truth,
    load_moodle_resource_inventory,
    validate_moodle_capability_contract,
)


REPO = Path(__file__).resolve().parents[2]
def test_human_only_binding_matches_current_topology() -> None:
    scenario = load_moodle_ground_truth()["SEC-01"]
    catalog = ActionCatalog.load_moodle_catalog()
    scope = "staging_database_security_group"
    assert scenario["fault_trigger"]["type"] == "rds_ingress_unauthorized_source"
    assert scenario["fault_trigger"]["target"] == scope
    action = scenario["allowed_remediation"][0]["action"]
    assert catalog.lookup(action) is not None
    assert not catalog.is_safe_execution_allowed(
        action, scenario_id="SEC-01", target_scope=scope, environment="staging", role="executor",
    )


def test_capability_contract_remains_valid_after_adaptation() -> None:
    assert validate_moodle_capability_contract(
        load_moodle_capability_contract(),
        load_moodle_resource_inventory(),
        load_moodle_ground_truth(),
    ) == []


def test_security_fault_is_human_only_and_never_public() -> None:
    scenario = json.loads((REPO / "evaluation/ground_truth/moodle/SEC-01.json").read_text(encoding="utf-8"))
    assert scenario["execution_policy"] == "HUMAN_ONLY"
    assert "not a public CIDR" in scenario["description"]
    assert ActionCatalog.load_moodle_catalog().lookup("remove_tagged_database_ingress_rule").permission.required_role == "operator"


def test_inventory_does_not_claim_a_reverse_proxy_service() -> None:
    assert "moodle-proxy" not in {resource["resource_id"] for resource in load_moodle_resource_inventory()["resources"]}


def test_moodle_contract_rejects_missing_scenario_requirements() -> None:
    contract = load_moodle_capability_contract()
    inventory = load_moodle_resource_inventory()
    scenarios = load_moodle_ground_truth()
    del scenarios["DB-01"]["fault_trigger"]

    errors = validate_moodle_capability_contract(contract, inventory, scenarios)

    assert any("DB-01 requires non-empty fault_trigger object" in error for error in errors)


def test_moodle_contract_reports_malformed_objects_without_crashing() -> None:
    contract = load_moodle_capability_contract()
    inventory = load_moodle_resource_inventory()
    scenarios = load_moodle_ground_truth()
    scenarios["DB-01"].update(expected_root_cause=None, expected_impact=None, rollback_plan=None)

    errors = validate_moodle_capability_contract(contract, inventory, scenarios)

    assert any("DB-01 requires non-empty expected_root_cause object" in error for error in errors)
    assert any("DB-01 requires non-empty expected_impact object" in error for error in errors)
    assert any("DB-01 requires non-empty rollback_plan object" in error for error in errors)


def test_moodle_contract_handles_unhashable_communication_entries() -> None:
    contract = load_moodle_capability_contract()
    inventory = load_moodle_resource_inventory()
    scenarios = load_moodle_ground_truth()
    scenarios["DB-01"]["communication_contract"]["allowed"] = [{}]

    errors = validate_moodle_capability_contract(contract, inventory, scenarios)

    assert any("communication_contract.allowed must be a non-empty string list" in error for error in errors)


def test_moodle_contract_returns_errors_for_malformed_nested_contract_lists() -> None:
    contract = load_moodle_capability_contract()
    inventory = load_moodle_resource_inventory()
    scenarios = load_moodle_ground_truth()
    contract["evidence_contract"]["required_fields"] = None
    contract["capabilities"][0]["resource_ids"] = None
    first_scope = next(iter(contract["target_scopes"].values()))
    first_scope["resource_ids"] = {"unhashable": []}

    errors = validate_moodle_capability_contract(contract, inventory, scenarios)

    assert any("evidence_contract.required_fields must be a non-empty string list" in error for error in errors)
    assert any("resource_ids must be a non-empty string list" in error for error in errors)


def test_moodle_contract_rejects_duplicate_and_malformed_resources() -> None:
    contract = load_moodle_capability_contract()
    inventory = load_moodle_resource_inventory()
    scenarios = load_moodle_ground_truth()
    inventory["resources"].append(dict(inventory["resources"][0]))
    inventory["resources"].append({"display_name": "missing ID"})

    errors = validate_moodle_capability_contract(contract, inventory, scenarios)

    assert any("duplicate resource_id:" in error for error in errors)
    assert any("resource inventory item" in error and "resource_id" in error for error in errors)


def test_moodle_contract_rejects_non_staging_scenario_environment() -> None:
    contract = load_moodle_capability_contract()
    inventory = load_moodle_resource_inventory()
    scenarios = load_moodle_ground_truth()
    scenarios["DB-01"]["initial_state"]["environment"] = "production"

    errors = validate_moodle_capability_contract(contract, inventory, scenarios)

    assert any("DB-01 initial_state environment must be staging" in error for error in errors)


def test_moodle_ground_truth_loader_rejects_filename_id_mismatch(tmp_path: Path) -> None:
    scenario = json.loads((REPO / "evaluation/ground_truth/moodle/DB-01.json").read_text(encoding="utf-8"))
    scenario["scenario_id"] = "NET-01"
    (tmp_path / "DB-01.json").write_text(json.dumps(scenario), encoding="utf-8")

    with pytest.raises(MoodleContractError, match="does not match filename"):
        load_moodle_ground_truth(tmp_path)
