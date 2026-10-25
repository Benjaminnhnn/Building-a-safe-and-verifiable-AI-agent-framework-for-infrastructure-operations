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
from core.safety.action_catalog import ActionCatalog
from evaluation.agent_replay.coordination.orchestrator import CheckpointOrchestrator
from evaluation.agent_replay.replay import observations_from_scenario
from core.schema.resource import Resource
from core.schema.scenario import ScenarioGroundTruth

SCENARIO_PREFIXES = ("DB-", "RES-", "NET-", "CON-", "SEC-")
ROLLBACK_DENIAL_REASON = "catalogued mutation requires a verified reciprocal rollback"
HUMAN_ONLY_REASON = "catalogued action requires an operator and cannot be automated"
OUTCOME_VERIFIED = "verified_dry_run"
OUTCOME_FAIL_CLOSED = "fail_closed_missing_rollback"
OUTCOME_HUMAN_ONLY = "fail_closed_human_only"
OUTCOME_ESCALATED = "escalated_without_action"
OUTCOME_INVALID = "invalid"


def _offline_and_unexecuted(report: dict[str, object]) -> bool:
    binding = report.get("scenario_binding")
    return (
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


def lacks_reciprocal_rollback(catalog_action_id: object) -> bool:
    """Recompute whether a reversible catalog mutation lacks its reciprocal inverse."""
    catalog = ActionCatalog.load_moodle_catalog()
    entry = catalog.lookup(catalog_action_id if isinstance(catalog_action_id, str) else "")
    if entry is None or entry.adapter == "probe" or not entry.is_reversible:
        return False
    inverse = catalog.lookup(entry.rollback_action or "")
    return not (
        inverse is not None
        and inverse.rollback_action == entry.action_id
        and inverse.adapter == entry.adapter
    )


def requires_operator(catalog_action_id: object) -> bool:
    """Recompute whether the catalog reserves this action for an operator."""
    catalog = ActionCatalog.load_moodle_catalog()
    entry = catalog.lookup(catalog_action_id if isinstance(catalog_action_id, str) else "")
    return entry is not None and entry.permission.required_role == "operator"


def _execution_succeeded(report: dict[str, object]) -> object:
    return report.get("execution_succeeded", report.get("execution_success"))


def _is_valid_verified_dry_run(report: dict[str, object]) -> bool:
    return (
        _offline_and_unexecuted(report)
        and report.get("incident_status") == "verified_dry_run"
        and report.get("gate_decision") == "allow"
        and report.get("execution_dry_run") is True
        and _execution_succeeded(report) is True
        and report.get("verification_run") is True
        and report.get("verification_simulated") is True
    )


def _is_fail_closed_rollback_denial(report: dict[str, object]) -> bool:
    catalog_action_id = report.get("catalog_action_id")
    return (
        _offline_and_unexecuted(report)
        and report.get("incident_status") == "gated"
        and report.get("gate_decision") == "deny"
        and report.get("gate_reasons") == [ROLLBACK_DENIAL_REASON]
        and report.get("catalog_action_lacks_reciprocal_rollback") is True
        and lacks_reciprocal_rollback(catalog_action_id)
        and _execution_succeeded(report) is False
        and report.get("execution_dry_run") is True
        and report.get("execution_error") == "pipeline_denied"
    )


def _is_fail_closed_human_only(report: dict[str, object]) -> bool:
    catalog_action_id = report.get("catalog_action_id")
    return (
        _offline_and_unexecuted(report)
        and report.get("incident_status") == "gated"
        and report.get("gate_decision") == "human_only"
        and report.get("gate_reasons") == [HUMAN_ONLY_REASON]
        and report.get("catalog_action_requires_operator") is True
        and requires_operator(catalog_action_id)
        and _execution_succeeded(report) is False
        and report.get("execution_error") == "pipeline_paused:human_only"
    )


def _is_escalated_without_action(report: dict[str, object]) -> bool:
    return (
        _offline_and_unexecuted(report)
        and report.get("incident_status") == "escalated"
        and report.get("action_type") is None
        and report.get("gate_decision") is None
        and report.get("execution_dry_run") is None
        and _execution_succeeded(report) is False
        and report.get("verification_run") is False
    )


def replay_outcome(report: dict[str, object]) -> str:
    if _is_valid_verified_dry_run(report):
        return OUTCOME_VERIFIED
    if _is_fail_closed_rollback_denial(report):
        return OUTCOME_FAIL_CLOSED
    if _is_fail_closed_human_only(report):
        return OUTCOME_HUMAN_ONLY
    if _is_escalated_without_action(report):
        return OUTCOME_ESCALATED
    return OUTCOME_INVALID


def _is_valid_offline_result(report: dict[str, object]) -> bool:
    return replay_outcome(report) != OUTCOME_INVALID


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
                    "catalog_action_id": (
                        result.action.parameters.get("catalog_action_id")
                        if result.action is not None
                        else None
                    ),
                    "gate_decision": decision,
                    "gate_reasons": list(result.safety.reasons) if result.safety else [],
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
                    "execution_error": (
                        result.execution_result.error
                        if result.execution_result is not None
                        else None
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
            reports[-1]["catalog_action_lacks_reciprocal_rollback"] = lacks_reciprocal_rollback(
                reports[-1]["catalog_action_id"]
            )
            reports[-1]["catalog_action_requires_operator"] = requires_operator(
                reports[-1]["catalog_action_id"]
            )
            reports[-1]["outcome"] = replay_outcome(reports[-1])

    accepted = [report for report in reports if report["outcome"] != OUTCOME_INVALID]
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
        "passed_count": len(accepted),
        "verified_dry_run_count": len(verified_dry_runs),
        "safely_gated_count": len(safely_gated),
        "safely_escalated_count": len(safely_escalated),
        "fail_closed_denial_count": sum(r["outcome"] == OUTCOME_FAIL_CLOSED for r in reports),
        "human_only_count": sum(r["outcome"] == OUTCOME_HUMAN_ONLY for r in reports),
        "escalated_without_action_count": sum(r["outcome"] == OUTCOME_ESCALATED for r in reports),
        "missing_reciprocal_rollback_count": sum(
            r["catalog_action_lacks_reciprocal_rollback"] is True for r in reports
        ),
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
