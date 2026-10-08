#!/usr/bin/env python3
"""Run the merged unified AI control plane without mutating infrastructure."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.evidence_store import SQLiteEvidenceStore
from core.moodle_contract import build_moodle_scenario_binding, load_moodle_ground_truth
from core.orchestrator import CheckpointOrchestrator
from core.replay import observations_from_scenario
from core.schema.resource import Resource
from core.schema.scenario import ScenarioGroundTruth

SCENARIO_PREFIXES = ("DB-", "RES-", "NET-", "CON-", "SEC-")


def _is_valid_offline_result(report: dict[str, object]) -> bool:
    binding = report.get("scenario_binding")
    return (
        report.get("error") is None
        and report.get("incident_status") == "verified_dry_run"
        and report.get("resolved_by_verifier") is False
        and report.get("execution_dry_run") is True
        and report.get("simulated") is True
        and report.get("pipeline_mode") == "offline_simulation"
        and report.get("live_verification_executed") is False
        and report.get("recovery_claim_scope") == "simulated_fixture_only"
        and isinstance(binding, dict)
        and binding.get("execution_permitted_by_binding") is False
    )


def _has_forbidden_execution(report: dict[str, object]) -> bool:
    binding = report.get("scenario_binding")
    return (
        report.get("execution_dry_run") is False
        or report.get("resolved_by_verifier") is True
        or report.get("live_verification_executed") is True
        or (
            isinstance(binding, dict)
            and binding.get("execution_permitted_by_binding") is True
        )
    )


def _load_resources() -> list[Resource]:
    path = REPO_ROOT / "evaluation" / "resources" / "moodle_resource_inventory.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Resource(**item) for item in payload["resources"]]


def _load_scenarios() -> dict[str, ScenarioGroundTruth]:
    root = REPO_ROOT / "evaluation" / "ground_truth"
    paths = sorted(
        path
        for path in root.glob("*.json")
        if path.name.startswith(SCENARIO_PREFIXES)
    )
    scenarios = [
        ScenarioGroundTruth(**json.loads(path.read_text(encoding="utf-8")))
        for path in paths
    ]
    return {scenario.scenario_id: scenario for scenario in scenarios}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario",
        nargs="?",
        default="all",
        help="one benchmark scenario ID or all (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="artifact directory; defaults to a timestamped directory under terraform/.artifacts",
    )
    args = parser.parse_args()

    catalog = _load_scenarios()
    if args.scenario == "all":
        selected = [catalog[key] for key in sorted(catalog)]
    elif args.scenario in catalog:
        selected = [catalog[args.scenario]]
    else:
        parser.error(
            f"unknown scenario {args.scenario!r}; choose one of: "
                + ", ".join([*sorted(catalog), "all"])
        )

    ground_truth_bindings = load_moodle_ground_truth(
        REPO_ROOT / "evaluation" / "ground_truth" / "moodle"
    )
    scenario_bindings = {
        scenario.scenario_id: build_moodle_scenario_binding(
            ground_truth_bindings[scenario.scenario_id]
        )
        for scenario in selected
    }

    campaign = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or (
        REPO_ROOT / "terraform" / ".artifacts" / "aiops-unified-replay" / campaign
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "evidence.db"
    resources = _load_resources()
    reports: list[dict[str, object]] = []

    with SQLiteEvidenceStore(database_path) as store:
        orchestrator = CheckpointOrchestrator(store)
        for scenario in selected:
            result = orchestrator.run_scenario(
                scenario,
                resources,
                observations=observations_from_scenario(scenario),
                run_id=f"replay-{scenario.scenario_id.lower()}",
            )
            action_type = result.action.action_type.value if result.action else None
            decision = result.safety.decision.value if result.safety else None
            reports.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "stages": [stage.value for stage in result.stages],
                    "incident_status": result.incident.status.value,
                    "resolved_by_verifier": result.incident.resolved_by_verifier,
                    "action_type": action_type,
                    "gate_decision": decision,
                    "execution_dry_run": (
                        result.execution_result.dry_run
                        if result.execution_result is not None
                        else None
                    ),
                    "simulated": result.simulated,
                    "pipeline_mode": "offline_simulation",
                    "scenario_binding": scenario_bindings[scenario.scenario_id],
                    "live_verification_executed": False,
                    "recovery_claim_scope": "simulated_fixture_only",
                    "error": result.error,
                }
            )

    failed = [report for report in reports if not _is_valid_offline_result(report)]
    summary = {
        "mode": "offline_replay",
        "live_infrastructure_claim": False,
        "scenario_count": len(reports),
        "passed_count": len(reports) - len(failed),
        "failed_count": len(failed),
        "forbidden_live_execution_count": sum(
            _has_forbidden_execution(report) for report in reports
        ),
        "live_verification_executed": any(
            report["live_verification_executed"] for report in reports
        ),
        "staging_live_allowlisted_scenario_count": sum(
            binding["execution_lane"] == "staging_live_allowlisted"
            for binding in scenario_bindings.values()
        ),
        "offline_or_shadow_only_scenario_count": sum(
            binding["execution_lane"] == "offline_or_shadow_only"
            for binding in scenario_bindings.values()
        ),
        "evidence_database": str(database_path),
        "reports": reports,
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"Unified AI replay: {summary['passed_count']}/{summary['scenario_count']} passed; "
        f"report={report_path}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
