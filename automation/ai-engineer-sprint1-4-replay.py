#!/usr/bin/env python3
"""Smoke-test the Moodle orchestration wiring without scoring agent quality.

This fixture-only check is not an empirical replay: ground-truth answers and
remediation allowlists are validated but never supplied to the agent stages.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.ground_truth import load_ground_truth, validate_ground_truth  # noqa: E402
from core.unified_core import DiagnosisAgent, ObserverAgent, PlannerAgent, SQLiteEvidenceStore, SequentialOrchestrator  # noqa: E402


def replay(root: Path, output: Path) -> dict:
    fixtures = sorted(root.glob("*.json"))
    output.mkdir(parents=True, exist_ok=True)
    store = SQLiteEvidenceStore(output / "evidence.sqlite3")
    observer, diagnosis, planner = ObserverAgent(store), DiagnosisAgent(), PlannerAgent()
    results = []
    for path in fixtures:
        truth = load_ground_truth(path)
        errors = validate_ground_truth(truth, path=path)
        if errors:
            results.append({"scenario_id": truth.get("scenario_id", path.stem), "status": "invalid_fixture", "errors": errors})
            continue
        sid = truth["scenario_id"]
        incident_id = f"replay-{sid}"
        event = {"event_id": f"evt-{sid}", "fingerprint": f"moodle-{sid}", "event_type": "service_health_failed", "source": "synthetic", "severity": "critical", "incident_id": incident_id}
        # A fixture can provide observed symptoms, but its expected cause and
        # allowed remediation are evaluation-only and must not enter decisions.
        signals = list(truth["observed_signals"])
        resource_id = "unclassified"
        observed = observer.observe(event, resource_id=resource_id)
        evidence_refs = list(observed["evidence_refs"])
        for kind, payload in (("metric", {"signals": signals}), ("topology", {"resource_id": resource_id})):
            evidence = store.append(incident_id=incident_id, source="fixture", resource_id=resource_id, kind=kind, payload=payload)
            evidence_refs.append(evidence.evidence_id)

        def run_observer(_context):
            return observed

        def run_diagnosis(_context):
            # This is deliberately an unclassified investigation candidate,
            # not a diagnosis derived from the expected-answer fixture.
            result = diagnosis.diagnose(
                signals=signals,
                hypotheses=[{"root_cause": {"component": "unclassified", "cause": "requires_operator_review"}, "signals": signals}],
                evidence_refs=evidence_refs,
            )
            return result

        def run_planner(context):
            if context["diagnosis"].get("status") != "ok":
                raise ValueError("diagnosis lacks supporting evidence")
            actions = planner.plan(incident_id=incident_id, environment="staging", resource_id=resource_id, allowed_actions=[], evidence_refs=evidence_refs)
            return {"actions": [action.model_dump(mode="json") for action in actions]}

        handlers = {
            "observer": run_observer,
            "diagnosis": run_diagnosis,
            "planner": run_planner,
            "gate": lambda _c: {"decision": "dry_run_only", "execution_permitted": False},
            "execution": lambda _c: {"status": "not_executed", "mutated": False},
            "verification": lambda _c: {"status": "not_run", "resolution_eligible": False},
        }
        messages = SequentialOrchestrator(store).run(incident_id=incident_id, handlers=handlers, context={"evidence_refs": evidence_refs})
        results.append({"scenario_id": sid, "status": "passed" if len(messages) == 6 and all(m.status == "ok" for m in messages) else "failed", "stages": [m.stage for m in messages], "stage_statuses": [m.status for m in messages], "evidence_count": len(evidence_refs), "execution": "not_executed", "resolution_eligible": False})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "fixture_orchestration_smoke", "empirical": False, "quality_scored": False, "scenario_count": len(fixtures), "passed": sum(item["status"] == "passed" for item in results), "failed": sum(item["status"] != "passed" for item in results), "mutations": 0, "resolution_eligible": 0, "limitations": ["Ground-truth expected causes and remediation allowlists are not passed to agent stages.", "This smoke test does not measure RCA accuracy, remediation quality, recovery, or live behavior."], "results": results}
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", type=Path, default=REPO_ROOT / "evaluation" / "ground_truth" / "moodle")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "terraform" / ".artifacts" / "ai-sprint1-4-replay")
    args = parser.parse_args()
    report = replay(args.ground_truth, args.output_dir)
    print(json.dumps({key: report[key] for key in ("mode", "scenario_count", "passed", "failed", "mutations", "resolution_eligible")}, indent=2))
    return 0 if report["failed"] == 0 and report["scenario_count"] == 15 else 1


if __name__ == "__main__":
    raise SystemExit(main())
