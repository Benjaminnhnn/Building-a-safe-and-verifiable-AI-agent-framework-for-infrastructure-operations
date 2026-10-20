"""Celery background task and CLI entrypoint for Independent Verification.

Invariants:
1. Runs in a separate Celery task or process context with read-only probe authority.
2. Zero imports from any executor module or function.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from core.celery_app import celery_app
from evaluation.agent_replay.verification.communication_contract import load_communication_contract
from evaluation.agent_replay.verification.independent_verifier import IndependentVerifier, RQ2DivergenceTracker

logger = logging.getLogger(__name__)


@celery_app.task(name="core.verification.verify_remediation_task", bind=True)
def verify_remediation_task(
    self: Any,
    *,
    contract_path: str,
    incident_id: str,
    scenario_id: str,
    plan_hash: str = "UNKNOWN",
    n_consecutive_checks: int = 3,
    interval_seconds: float = 0.0,
    divergence_metrics_path: str | None = None,
) -> dict[str, Any]:
    """Execute independent verification as an isolated Celery task."""
    logger.info(
        f"Starting Celery verification task for incident {incident_id}, scenario {scenario_id}"
    )
    contract = load_communication_contract(contract_path)
    tracker = RQ2DivergenceTracker(metrics_file=divergence_metrics_path) if divergence_metrics_path else None

    verifier = IndependentVerifier(
        contract=contract,
        n_consecutive_checks=n_consecutive_checks,
        interval_seconds=interval_seconds,
        divergence_tracker=tracker,
    )

    report = verifier.verify(
        incident_id=incident_id,
        scenario_id=scenario_id,
        plan_hash=plan_hash,
    )

    return report.model_dump(mode="json")


def main() -> None:
    """CLI process entrypoint for running independent verification as a standalone process."""
    parser = argparse.ArgumentParser(description="Run Independent Verifier in a standalone process")
    parser.add_argument("--contract", required=True, help="Path to communication contract YAML")
    parser.add_argument("--incident-id", required=True, help="Incident identifier")
    parser.add_argument("--scenario-id", required=True, help="Scenario identifier")
    parser.add_argument("--plan-hash", default="UNKNOWN", help="Plan hash for audit trail")
    parser.add_argument("--checks", type=int, default=3, help="Number of consecutive stable checks")
    parser.add_argument("--metrics-file", default=None, help="Path to save RQ2 divergence metrics JSON")

    args = parser.parse_args()
    contract = load_communication_contract(args.contract)
    tracker = RQ2DivergenceTracker(metrics_file=args.metrics_file) if args.metrics_file else None

    verifier = IndependentVerifier(
        contract=contract,
        n_consecutive_checks=args.checks,
        divergence_tracker=tracker,
    )

    report = verifier.verify(
        incident_id=args.incident_id,
        scenario_id=args.scenario_id,
        plan_hash=args.plan_hash,
    )

    print(json.dumps(report.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()

