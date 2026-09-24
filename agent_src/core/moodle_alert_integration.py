"""Fail-closed shadow integration for explicitly identified Moodle staging alerts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core.ground_truth import ground_truth_dir, load_ground_truth, validate_ground_truth
from core.unified_core import DiagnosisAgent, ObserverAgent, PlannerAgent, SQLiteEvidenceStore, SequentialOrchestrator


def _load_catalog() -> dict[str, dict[str, Any]]:
    root = ground_truth_dir() / "moodle"
    catalog: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        item = load_ground_truth(path)
        errors = validate_ground_truth(item, path=path)
        if errors:
            raise ValueError(f"invalid Moodle ground truth {path.name}: {errors}")
        catalog[item["scenario_id"]] = item
    return catalog


def process_moodle_alert(alert: dict[str, Any]) -> dict[str, Any] | None:
    """Run the unified AI stages for a matching, explicitly labeled staging alert.

    This function never executes infrastructure changes. Unknown or mismatched
    scenario signals return a non-executable report; live mode is rejected.
    """
    mode = os.getenv("AIOPS_UNIFIED_CORE_MODE", "disabled").strip().lower()
    labels = alert.get("labels") or {}
    if mode not in {"shadow", "live"}:
        return None
    if mode == "live":
        raise RuntimeError("live execution is not supported by the Alertmanager integration; use shadow mode")
    if alert.get("status") != "firing":
        return None
    if labels.get("service") != "moodle" or labels.get("environment") != "staging":
        return None

    scenario_id = str(labels.get("scenario_id") or "")
    catalog = _load_catalog()
    truth = catalog.get(scenario_id)
    alert_name = str(labels.get("alertname") or "")
    if truth is None:
        return {"status": "escalated", "reason": "missing or unknown scenario_id label", "execution_permitted": False, "resolution_eligible": False}
    if alert_name not in set(truth["observed_signals"]):
        return {"status": "escalated", "scenario_id": scenario_id, "reason": "alert signal does not match scenario ground truth", "execution_permitted": False, "resolution_eligible": False}

    fingerprint = str(alert.get("fingerprint") or alert.get("event_id") or "")
    if not fingerprint:
        return {"status": "escalated", "scenario_id": scenario_id, "reason": "alert has no normalized fingerprint", "execution_permitted": False, "resolution_eligible": False}
    incident_id = "moodle-shadow-" + hashlib.sha256(f"{scenario_id}:{fingerprint}".encode()).hexdigest()[:20]
    resource_id = str(labels.get("component") or labels.get("instance") or "moodle-staging")
    db_path = Path(os.getenv("AIOPS_EVIDENCE_DB", "./aiops-evidence/evidence.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteEvidenceStore(db_path)
    observer = ObserverAgent(store)
    observed = observer.observe({**alert, "incident_id": incident_id}, resource_id=resource_id)
    refs = list(observed["evidence_refs"])
    safe_signal = {"alertname": alert_name, "severity": labels.get("severity"), "scenario_id": scenario_id}
    refs.append(store.append(incident_id=incident_id, source="alertmanager", resource_id=resource_id, kind="alert_signal", payload=safe_signal).evidence_id)
    refs.append(store.append(incident_id=incident_id, source="ground_truth_contract", resource_id=resource_id, kind="scenario_contract", payload={"scenario_id": scenario_id, "root_cause": truth["expected_root_cause"]}).evidence_id)

    diagnosis = DiagnosisAgent()
    planner = PlannerAgent()

    def plan(context: dict[str, Any]) -> dict[str, Any]:
        if context["diagnosis"].get("status") != "ok" or not context["diagnosis"].get("top1"):
            raise ValueError("diagnosis lacks matching signal evidence")
        actions = planner.plan(incident_id=incident_id, environment="staging", resource_id=resource_id, allowed_actions=truth["allowed_remediation"], evidence_refs=refs)
        return {"actions": [action.model_dump(mode="json") for action in actions]}

    handlers = {
        "observer": lambda _context: observed,
        "diagnosis": lambda _context: diagnosis.diagnose(signals=[alert_name], hypotheses=[{"root_cause": truth["expected_root_cause"], "signals": [alert_name]}], evidence_refs=refs),
        "planner": plan,
        "gate": lambda _context: {"decision": "shadow_only", "execution_permitted": False},
        "execution": lambda _context: {"status": "not_executed", "mutated": False},
        "verification": lambda _context: {"status": "not_run", "resolution_eligible": False},
    }
    messages = SequentialOrchestrator(store).run(incident_id=incident_id, handlers=handlers, context={"evidence_refs": refs})
    success = len(messages) == 6 and all(message.status == "ok" for message in messages)
    return {
        "status": "shadow_complete" if success else "escalated",
        "incident_id": incident_id,
        "scenario_id": scenario_id,
        "stages": [{"stage": message.stage, "status": message.status} for message in messages],
        "evidence_refs": refs,
        "duplicate_alert": observed["duplicate"],
        "execution_permitted": False,
        "resolution_eligible": False,
    }
