#!/usr/bin/env python3
"""Run the merged unified AI control plane without mutating infrastructure."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.evidence.evidence_store import SQLiteEvidenceStore
from core.moodle.contract import build_moodle_scenario_binding, load_moodle_ground_truth
from evaluation.agent_replay.coordination.orchestrator import CheckpointOrchestrator
from evaluation.agent_replay.replay import observations_from_scenario
from core.schema.resource import Resource
from core.schema.scenario import ScenarioGroundTruth

SCENARIO_PREFIXES = ("DB-", "RES-", "NET-", "CON-", "SEC-")


def _is_valid_offline_result(report: dict[str, object]) -> bool:
    binding = report.get("scenario_binding")
    common = (
        report.get("error") is None
        and report.get("resolved_by_verifier") is False
        and report.get("simulated") is True
        and report.get("pipeline_mode") == "offline_simulation"
        and report.get("live_verification_executed") is False
        and report.get("recovery_claim_scope") == "simulated_fixture_only"
        and report.get("execution_executed") is False
        and isinstance(binding, dict)
        and binding.get("execution_permitted_by_binding") is False
    )
    if not common:
        return False

    if report.get("incident_status") == "verified_dry_run":
        return (
            report.get("gate_decision") == "allow"
            and report.get("execution_dry_run") is True
            and report.get("execution_succeeded") is True
            and report.get("verification_run") is True
            and report.get("verification_simulated") is True
        )

    if report.get("incident_status") == "escalated":
        return (
            report.get("stages") in (
                ["observe", "triage", "diagnose"],
                ["observe", "triage", "diagnose", "plan"],
            )
            and report.get("action_type") is None
            and report.get("gate_decision") is None
            and report.get("execution_dry_run") is None
            and report.get("execution_succeeded") is False
            and report.get("verification_run") is False
        )

    return (
        report.get("incident_status") == "gated"
        and report.get("gate_decision") in {"deny", "require_approval", "human_only"}
        and report.get("execution_dry_run") is True
        and report.get("execution_succeeded") is False
        and report.get("verification_run") is False
    )


def _has_forbidden_execution(report: dict[str, object]) -> bool:
    binding = report.get("scenario_binding")
    return (
        report.get("execution_dry_run") is False
        or report.get("execution_executed") is True
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
                    "execution_executed": (
                        not result.execution_result.dry_run
                        if result.execution_result is not None
                        else False
                    ),
                    "execution_succeeded": (
                        result.execution_result.success
                        if result.execution_result is not None
                        else False
                    ),
                    "verification_run": result.verification is not None,
                    "verification_simulated": (
                        result.verification.simulated
                        if result.verification is not None
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

    accepted = [report for report in reports if _is_valid_offline_result(report)]
    verified_dry_runs = [
        report for report in accepted
        if report["incident_status"] == "verified_dry_run"
    ]
    safely_gated = [report for report in accepted if report["incident_status"] == "gated"]
    safely_escalated = [report for report in accepted if report["incident_status"] == "escalated"]
    summary = {
        "mode": "offline_replay",
        "live_infrastructure_claim": False,
        "accepted_outcomes_are_not_recovery_results": True,
        "recovery_claim_count": 0,
        "scenario_count": len(reports),
        "accepted_count": len(accepted),
        "verified_dry_run_count": len(verified_dry_runs),
        "safely_gated_count": len(safely_gated),
        "safely_escalated_count": len(safely_escalated),
        "failed_count": len(reports) - len(accepted),
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
        f"Unified AI replay: {summary['accepted_count']}/{summary['scenario_count']} accepted "
        f"({summary['verified_dry_run_count']} verified dry-runs, "
        f"{summary['safely_gated_count']} safely gated, "
        f"{summary['safely_escalated_count']} safely escalated); "
        f"report={report_path}"
    )
    return 1 if summary["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
