"""Moodle staging alert integration — shadow and live pipeline routing.

Shadow mode (AIOPS_UNIFIED_CORE_MODE=shadow):
    Records the alert and collects bounded read-only evidence. It does not use
    ground-truth answers to diagnose a live event or request an action.

Live mode (AIOPS_UNIFIED_CORE_MODE=live):
    Delegates to MoodleIncidentPipeline which enforces the Safety Gate, requires
    MOODLE_PIPELINE_EXECUTION_CONFIRM=staging, and runs the Independent Verifier.
    Execution only occurs when the gate decision is ALLOW.

Disabled mode (default):
    Returns None; the caller falls through to the legacy pipeline.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from core.ground_truth import ground_truth_dir, load_ground_truth, validate_ground_truth

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

    if mode == "shadow":
        # Scenario IDs are useful for evidence attribution but are never used as
        # an oracle for a live diagnosis. Ground truth is reserved for offline
        # replay/scoring; the shadow path uses independent runtime collectors.
        from core.shadow_pipeline import run_shadow_if_enabled

        observation = run_shadow_if_enabled(alert)
        if observation is None:
            return None
        if observation.get("status") != "observed":
            return {
                "status": "escalated",
                "scenario_id": labels.get("scenario_id"),
                "reason": observation.get("reason", "observer could not map resource"),
                "execution_permitted": False,
                "resolution_eligible": False,
            }
        evidence_status = observation.get("evidence_status", "partial")
        return {
            "status": "shadow_observed" if evidence_status == "complete" else "awaiting_evidence",
            "incident_id": observation["incident_id"],
            "scenario_id": labels.get("scenario_id"),
            "stage": observation.get("stage"),
            "evidence_status": evidence_status,
            "evidence_refs": observation.get("evidence_refs", []),
            "evidence_sources": observation.get("evidence_sources", []),
            "collector_errors": observation.get("collector_errors", []),
            "execution_permitted": False,
            "resolution_eligible": False,
        }

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

    # The code below is only reachable for explicitly enabled live mode.
    raise AssertionError("shadow routing must return before live catalog loading")
