#!/usr/bin/env python3
"""Offline, non-mutating demonstration of Moodle Safe Gate and verifier.

This exercises the real SafeExecutionGate and VerificationAgent classes with
fixture evidence/probes. It does not send an alert, call Gemini, dispatch an
adapter, or change Moodle/AWS resources.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.adapters import SafeActionRequest  # noqa: E402
from core.safe_execution_gate import ApprovalRecord, SafeExecutionGate  # noqa: E402
from core.schema.action import RollbackPlan, TypedAction  # noqa: E402
from core.schema.common import ActionType, Environment  # noqa: E402
from core.schema.incident import Incident  # noqa: E402
from core.schema.verification import StabilityObservation  # noqa: E402
from core.verification_agent import DryRunProbe, VerificationAgent  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "terraform/.artifacts/sprint7-demo/safe-gate-verifier.json",
        help="Evidence path (default is ignored Terraform artifacts directory).",
    )
    args = parser.parse_args()

    # Synthetic incident and evidence IDs: no production alert or evidence is read.
    incident_id = "offline-demo-db01"
    action = TypedAction(
        action_id="offline-demo-action-db01",
        incident_id=incident_id,
        action_type=ActionType.REMOVE_SCOPED_PORT_BLOCK,
        target_resource_id="moodle-staging-db-path",
        environment=Environment.STAGING,
        reason="Offline fixture: reviewed DB-01 action candidate",
        evidence_refs=["fixture:prometheus", "fixture:db-probe", "fixture:network-rule"],
        expected_outcome="staging Moodle DB connectivity restored",
        reversible=True,
        rollback_plan=RollbackPlan(available=True, method="fixture-only"),
    )
    request = SafeActionRequest(
        request_id="offline-demo-request-db01",
        idempotency_key="offline-demo-idem-db01",
        catalog_action_id="remove_scoped_db_reject",
        scenario_id="DB-01",
        target_scope="staging_moodle_nodes",
        action=action,
        timeout_seconds=30,
    )

    # A demo-only signing key and actor. This is deliberately not an AWS or
    # deployment credential and cannot authorize the live executor API.
    demo_signing_key = "offline-only-demo-signing-key"
    gate = SafeExecutionGate(
        approval_signing_key=demo_signing_key,
        approved_actors={"demo-human-operator"},
    )
    awaiting_approval = gate.evaluate(
        request, actor_role="executor", confidence=0.72
    )
    approval = ApprovalRecord.create(
        action_sha256=gate.action_hash(request),
        actor_id="demo-human-operator",
        signing_key=demo_signing_key,
        ttl_seconds=600,
    )
    after_approval = gate.evaluate(
        request,
        actor_role="executor",
        confidence=0.72,
        approval=approval,
    )
    assert awaiting_approval.decision == "REQUIRE_APPROVAL"
    assert after_approval.decision == "ALLOW"

    incident = Incident(
        incident_id=incident_id,
        fingerprint="offline-demo-fingerprint",
        title="Offline DB-01 recovery verification fixture",
        severity="warning",
        affected_resources=["moodle-staging-db-path"],
    )
    contract = {
        "allowed": ["moodle_to_database"],
        "forbidden": ["internet_to_postgresql"],
        "related": ["prometheus_scrape"],
    }
    now = datetime.now(timezone.utc)
    stability = [
        StabilityObservation(observed_at=now - timedelta(seconds=120), healthy=True),
        StabilityObservation(observed_at=now - timedelta(seconds=60), healthy=True),
        StabilityObservation(observed_at=now, healthy=True),
    ]
    verifier = VerificationAgent()

    # False-recovery case: service health is good, but a forbidden path is open.
    false_recovery = verifier.verify(
        incident,
        contract,
        dry_run=True,
        probes=[
            DryRunProbe("moodle_to_database", "allowed", passes=True),
            DryRunProbe("internet_to_postgresql", "forbidden", passes=False),
            DryRunProbe("prometheus_scrape", "related", passes=True),
        ],
        stability_observations=stability,
        stability_window_seconds=120,
    )
    # Contract-complete fixture: all allowed, forbidden, related, and stability
    # checks pass. The result remains simulated because probes are fixtures.
    verified_fixture = verifier.verify(
        incident,
        contract,
        dry_run=True,
        probes=[
            DryRunProbe("moodle_to_database", "allowed", passes=True),
            DryRunProbe("internet_to_postgresql", "forbidden", passes=True),
            DryRunProbe("prometheus_scrape", "related", passes=True),
        ],
        stability_observations=stability,
        stability_window_seconds=120,
    )
    assert false_recovery.verdict == "false_recovery"
    assert not false_recovery.resolution_eligible
    assert verified_fixture.verdict == "resolved"
    assert verified_fixture.resolution_eligible and verified_fixture.simulated

    record = {
        "schema_version": "1.0",
        "demo_type": "offline_safe_gate_and_independent_verifier",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "synthetic fixtures only; no Gemini, alert webhook, adapter, AWS, or Moodle mutation",
        "proposal": {
            "source": "fixed demonstration fixture; not an Agent/Gemini proposal",
            "candidate_action": request.catalog_action_id,
            "scenario_id": request.scenario_id,
            "evidence_refs": action.evidence_refs,
        },
        "safe_gate": {
            "without_human_approval": awaiting_approval.model_dump(mode="json"),
            "with_action_bound_human_approval": after_approval.model_dump(mode="json"),
            "adapter_dispatched": False,
            "infrastructure_mutated": False,
        },
        "independent_verifier": {
            "health_only_false_recovery": {
                "verdict": false_recovery.verdict,
                "health_passed": false_recovery.health_passed,
                "communication_contract_passed": false_recovery.communication_contract_passed,
                "stability_seconds": false_recovery.stability_seconds,
                "resolution_eligible": false_recovery.resolution_eligible,
                "simulated": false_recovery.simulated,
            },
            "complete_contract_fixture": {
                "verdict": verified_fixture.verdict,
                "health_passed": verified_fixture.health_passed,
                "communication_contract_passed": verified_fixture.communication_contract_passed,
                "stability_seconds": verified_fixture.stability_seconds,
                "resolution_eligible": verified_fixture.resolution_eligible,
                "simulated": verified_fixture.simulated,
            },
        },
        "acceptance": {
            "gate_requires_approval_for_medium_confidence": True,
            "gate_requires_exact_action_bound_approval": after_approval.decision == "ALLOW",
            "health_only_does_not_resolve": not false_recovery.resolution_eligible,
            "contract_and_120s_stability_required": verified_fixture.resolution_eligible,
            "live_action_executed": False,
            "live_incident_resolved": False,
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Safe Gate, no approval: {awaiting_approval.decision}")
    print(f"Safe Gate, exact human approval: {after_approval.decision}")
    print("Adapter dispatch: none (no mutation)")
    print(
        "Independent Verifier, health green but forbidden path open: "
        f"{false_recovery.verdict}; resolution_eligible={false_recovery.resolution_eligible}"
    )
    print(
        "Independent Verifier, fixture contract + 120s stability: "
        f"{verified_fixture.verdict}; simulated={verified_fixture.simulated}"
    )
    print(f"Evidence: {args.output}")
    print("Classification: offline functional demo; not live remediation evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
