"""Moodle staging alert integration — shadow and live pipeline routing.

Shadow mode (AIOPS_UNIFIED_CORE_MODE=shadow):
    Runs the full Observer→Diagnosis→Planner sequence in dry-run, then returns
    a non-executable report.  Safe for any staging alert.

Live mode (AIOPS_UNIFIED_CORE_MODE=live):
    Delegates to MoodleIncidentPipeline which enforces the Safety Gate, requires
    MOODLE_PIPELINE_EXECUTION_CONFIRM=staging, and runs the Independent Verifier.
    Execution only occurs when the gate decision is ALLOW.

Disabled mode (default):
    Returns None; the caller falls through to the legacy pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from core.ground_truth import ground_truth_dir, load_ground_truth, validate_ground_truth
from core.unified_core import DiagnosisAgent, ObserverAgent, PlannerAgent, SQLiteEvidenceStore, SequentialOrchestrator

logger = logging.getLogger(__name__)


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


def _route_live(
    alert: dict[str, Any],
    scenario_id: str,
    truth: dict[str, Any],
    labels: dict[str, Any],
) -> dict[str, Any]:
    """Delegate to MoodleIncidentPipeline for live gate + execution + verification."""
    from core.moodle_pipeline import (
        MoodleExecutionAdapter,
        MoodleIncidentPipeline,
        PipelineError,
    )
    from core.event_schema import normalize_alert

    evidence_root = Path(os.getenv("AIOPS_EVIDENCE_DB", "./aiops-evidence/evidence.sqlite3")).parent

    adapter = MoodleExecutionAdapter(allow_live_execution=True)
    pipeline = MoodleIncidentPipeline(evidence_root, adapter=adapter)

    # Normalize the raw Alertmanager alert dict to the canonical event format
    try:
        event = normalize_alert(
            {
                "status": alert.get("status", "firing"),
                "fingerprint": alert.get("fingerprint") or alert.get("event_id") or "",
                "startsAt": alert.get("startsAt", ""),
                "labels": {
                    "alertname": str(labels.get("alertname") or ""),
                    "scenario_id": scenario_id,
                    "environment": "staging",
                    "severity": str(labels.get("severity") or "critical"),
                },
                "annotations": alert.get("annotations") or {},
            },
        )
    except Exception as exc:
        return {
            "status": "escalated",
            "scenario_id": scenario_id,
            "reason": f"alert normalization failed: {exc}",
            "execution_permitted": False,
            "resolution_eligible": False,
        }

    try:
        report = pipeline.process(
            event,
            scenario_id=scenario_id,
            mode="execute",
            live_verify=True,
        )
    except PipelineError as exc:
        return {
            "status": "escalated",
            "scenario_id": scenario_id,
            "reason": str(exc),
            "execution_permitted": False,
            "resolution_eligible": False,
        }
    except Exception as exc:
        logger.exception("Unexpected error in live Moodle pipeline: %s", exc)
        return {
            "status": "escalated",
            "scenario_id": scenario_id,
            "reason": f"pipeline error: {exc}",
            "execution_permitted": False,
            "resolution_eligible": False,
        }

    state = report.get("state", "UNKNOWN")
    return {
        "status": "live_complete" if state == "RESOLVED" else state.lower(),
        "incident_id": report.get("incident_id"),
        "scenario_id": scenario_id,
        "state": state,
        "evidence_refs": report.get("evidence_refs", []),
        "execution_permitted": report.get("execution") is not None,
        "resolution_eligible": state == "RESOLVED",
    }


def process_moodle_alert(alert: dict[str, Any]) -> dict[str, Any] | None:
    """Route a Moodle staging alert through shadow or live pipeline.

    Returns None if the pipeline is disabled or the alert is not a Moodle staging
    event — the caller falls through to the legacy processing path.
    """
    mode = os.getenv("AIOPS_UNIFIED_CORE_MODE", "disabled").strip().lower()
    labels = alert.get("labels") or {}

    if mode not in {"shadow", "live"}:
        return None
    if alert.get("status") != "firing":
        return None
    if labels.get("service") != "moodle" or labels.get("environment") != "staging":
        return None

    scenario_id = str(labels.get("scenario_id") or "")
    catalog = _load_catalog()
    truth = catalog.get(scenario_id)
    alert_name = str(labels.get("alertname") or "")

    if truth is None:
        return {
            "status": "escalated",
            "reason": "missing or unknown scenario_id label",
            "execution_permitted": False,
            "resolution_eligible": False,
        }
    if alert_name not in set(truth["observed_signals"]):
        return {
            "status": "escalated",
            "scenario_id": scenario_id,
            "reason": "alert signal does not match scenario ground truth",
            "execution_permitted": False,
            "resolution_eligible": False,
        }

    fingerprint = str(alert.get("fingerprint") or alert.get("event_id") or "")
    if not fingerprint:
        return {
            "status": "escalated",
            "scenario_id": scenario_id,
            "reason": "alert has no normalized fingerprint",
            "execution_permitted": False,
            "resolution_eligible": False,
        }

    # ── Live mode: delegate to MoodleIncidentPipeline ──────────────────────────
    if mode == "live":
        logger.info("Routing Moodle alert to live pipeline: scenario=%s alert=%s", scenario_id, alert_name)
        return _route_live(alert, scenario_id, truth, labels)

    # ── Shadow mode: unified core Observer→Diagnosis→Planner, no execution ──────
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
        actions = planner.plan(
            incident_id=incident_id,
            environment="staging",
            resource_id=resource_id,
            allowed_actions=truth["allowed_remediation"],
            evidence_refs=refs,
        )
        return {"actions": [action.model_dump(mode="json") for action in actions]}

    handlers = {
        "observer": lambda _context: observed,
        "diagnosis": lambda _context: diagnosis.diagnose(
            signals=[alert_name],
            hypotheses=[{"root_cause": truth["expected_root_cause"], "signals": [alert_name]}],
            evidence_refs=refs,
        ),
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
