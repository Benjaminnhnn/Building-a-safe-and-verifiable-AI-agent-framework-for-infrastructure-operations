#!/usr/bin/env python3
"""Run an offline Sprint 7 contract/safety demo without mutating infrastructure.

This demonstrates ERPNext ground-truth contracts, staging-only action-catalog
decisions, and the verifier's health-only counterfactual. It is deliberately
not an empirical runtime benchmark and never executes Docker/Ansible actions.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.action_catalog import ActionCatalog  # noqa: E402
from core.ground_truth import validate_ground_truth  # noqa: E402
from core.verifier_contract import ContractProbeRunner  # noqa: E402


SCENARIOS = {
    "ERP-01": ("start_erpnext_mariadb_container", "probe_erpnext_mariadb_connectivity"),
    "ERP-02": ("start_erpnext_redis_container", "probe_erpnext_redis_connectivity"),
    "ERP-03": ("restore_erpnext_nginx_config", "probe_erpnext_http_health"),
}


def scenario_record(scenario_id: str, catalog: ActionCatalog) -> dict[str, Any]:
    path = REPO_ROOT / "evaluation" / "ground_truth" / "erpnext" / f"{scenario_id}.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_ground_truth(fixture, path=path)
    remediation_id, probe_id = SCENARIOS[scenario_id]
    remediation = catalog.lookup(remediation_id)
    probe = catalog.lookup(probe_id)
    assert not errors, f"{scenario_id} fixture invalid: {errors}"
    assert remediation is not None, f"action missing from catalog: {remediation_id}"
    assert probe is not None, f"probe missing from catalog: {probe_id}"

    staging_allowed = catalog.is_allowed(remediation_id, environment="staging", role="executor")
    production_allowed = catalog.is_allowed(remediation_id, environment="production", role="executor")
    verifier_probe_allowed = catalog.is_allowed(probe_id, environment="staging", role="verifier")
    executor_probe_allowed = catalog.is_allowed(probe_id, environment="staging", role="executor")
    assert staging_allowed and not production_allowed
    assert verifier_probe_allowed and not executor_probe_allowed

    return {
        "scenario_id": scenario_id,
        "fixture_valid": True,
        "fault_target": fixture["fault_trigger"]["target"],
        "expected_root_cause": fixture["expected_root_cause"],
        "representative_signal": fixture["observed_signals"][0],
        "shadow_plan": {
            "remediation_action": remediation_id,
            "adapter": remediation.adapter,
            "staging_executor_decision": "ALLOW" if staging_allowed else "DENY",
            "production_executor_decision": "ALLOW" if production_allowed else "DENY",
            "live_mutation_performed": False,
        },
        "read_only_probe": {
            "action": probe_id,
            "staging_verifier_decision": "ALLOW" if verifier_probe_allowed else "DENY",
            "staging_executor_decision": "ALLOW" if executor_probe_allowed else "DENY",
        },
        "rollback_action": fixture["rollback_plan"]["action"],
        "result": "contract_demo_passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "terraform/.artifacts/sprint7-demo/offline-demo.json",
        help="Evidence path (default is ignored Terraform artifacts directory).",
    )
    args = parser.parse_args()

    catalog = ActionCatalog.load_erpnext_catalog()
    scenarios = [scenario_record(scenario_id, catalog) for scenario_id in SCENARIOS]

    # RQ1 safety-gate ablation is represented only as a shadow counterfactual:
    # forbidden/missing actions are recorded, never dispatched to an adapter.
    dangerous_action = "delete_database"
    gate_decision = "ALLOW" if catalog.is_allowed(
        dangerous_action, environment="staging", role="executor"
    ) else "DENY"
    no_gate = {
        "mode": "no_safety_gate",
        "shadow_only": True,
        "candidate_action": dangerous_action,
        "safety_gate_decision": gate_decision,
        "counterfactual_decision": "WOULD_EXECUTE_IF_SAFETY_GATE_REMOVED",
        "action_dispatched": False,
    }

    # RQ2 illustrates why health-only cannot establish recovery: health can be
    # green while required contract/stability probes remain absent.
    health_only_verdict = ContractProbeRunner().run_counterfactual_health_only(
        incident_id="sprint7-demo-health-only", health_passed=True
    )
    no_verifier = {
        "mode": "no_verifier",
        "health_only_passed": health_only_verdict.health_passed,
        "contract_passed": health_only_verdict.contract_passed,
        "stable": health_only_verdict.stable,
        "resolution_eligible": health_only_verdict.resolution_eligible,
        "reason": health_only_verdict.reason,
        "counterfactual_only": True,
    }
    assert no_gate["safety_gate_decision"] == "DENY" and not no_gate["action_dispatched"]
    assert no_verifier["health_only_passed"] and not no_verifier["resolution_eligible"]

    record = {
        "schema_version": "1.0",
        "demo_type": "offline_contract_and_safety_demo",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local code/fixture only; no ERPNext runtime, AWS, Docker, or Ansible actions",
        "scenarios": scenarios,
        "ablations": {"no_safety_gate": no_gate, "no_verifier": no_verifier},
        "acceptance": {
            "erpnext_contracts_valid": len(scenarios) == 3,
            "staging_only_executor_scope_demonstrated": True,
            "verifier_probe_role_separation_demonstrated": True,
            "dangerous_action_not_dispatched": True,
            "health_only_cannot_resolve": True,
            "empirical_erpnext_recovery_claim": False,
            "50_plus_50_ablation_requirement_met": False,
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for item in scenarios:
        print(
            f"{item['scenario_id']} PASS contract; "
            f"staging={item['shadow_plan']['staging_executor_decision']}; "
            f"production={item['shadow_plan']['production_executor_decision']}; "
            "mutation=none"
        )
    print(
        "ABLATION demo PASS: unsafe counterfactual logged, action not dispatched; "
        f"health_only_resolved={no_verifier['resolution_eligible']}"
    )
    print(f"Evidence: {args.output.relative_to(REPO_ROOT) if args.output.is_relative_to(REPO_ROOT) else args.output}")
    print("Classification: offline functional demo only; not empirical benchmark evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
