"""Moodle staging alert integration — shadow and live pipeline routing.

Shadow mode (AIOPS_UNIFIED_CORE_MODE=shadow):
    Records the alert and collects bounded read-only evidence. It does not use
    ground-truth answers to diagnose a live event or request an action.

Investigate mode (AIOPS_UNIFIED_CORE_MODE=investigate):
    Uses the evidence-derived diagnosis and may produce a read-only probe
    proposal. It cannot execute a proposal or resolve an incident.

Live mode (AIOPS_UNIFIED_CORE_MODE=live):
    Fails closed. The available end-to-end pipeline uses ground-truth answers to
    choose diagnosis, action, and verifier contract, so it is not valid for live
    decision-making or thesis evidence. Shadow observation remains available.

Disabled mode (default):
    Moodle alerts are escalated instead of falling through to the legacy path.
    Non-Moodle alerts retain their existing handling.
"""

from __future__ import annotations

import os
from typing import Any

from core.evidence_store import EvidenceIntegrityError


def process_moodle_alert(alert: dict[str, Any]) -> dict[str, Any] | None:
    """Route Moodle alerts through staging shadow mode or fail-closed escalation.

    Returns None only for non-Moodle or non-firing alerts; firing Moodle alerts
    never fall through to legacy handling.
    """
    mode = os.getenv("AIOPS_UNIFIED_CORE_MODE", "disabled").strip().lower()
    labels = alert.get("labels") or {}
    alert_name = str(labels.get("alertname") or "")

    if labels.get("service") != "moodle" and not alert_name.startswith("Moodle"):
        return None
    if alert.get("status") != "firing":
        return None
    environment = str(labels.get("environment") or "unknown").strip().lower()
    if environment != "staging":
        return {
            "status": "escalated",
            "scenario_id": labels.get("scenario_id"),
            "reason": f"Moodle AI processing is restricted to staging; received environment={environment}",
            "execution_permitted": False,
            "resolution_eligible": False,
        }

    if mode in {"shadow", "investigate"}:
        # Scenario IDs are useful for evidence attribution but are never used as
        # an oracle for a live diagnosis. Ground truth is reserved for offline
        # replay/scoring; the shadow path uses independent runtime collectors.
        from core.shadow_pipeline import run_shadow_if_enabled

        try:
            observation = run_shadow_if_enabled(alert)
        except EvidenceIntegrityError:
            return {
                "status": "escalated",
                "scenario_id": labels.get("scenario_id"),
                "reason": "stored evidence failed integrity validation; shadow diagnosis was stopped",
                "execution_permitted": False,
                "resolution_eligible": False,
            }
        if observation is None:
            return None
        if observation.get("status") == "in_progress":
            return {
                "status": "processing",
                "mode": mode,
                "incident_id": observation.get("incident_id"),
                "execution_permitted": False,
                "resolution_eligible": False,
            }
        if observation.get("status") != "observed":
            return {
                "status": "escalated",
                "scenario_id": labels.get("scenario_id"),
                "reason": observation.get("reason", "observer could not map resource"),
                "execution_permitted": False,
                "resolution_eligible": False,
            }
        diagnosis = observation.get("diagnosis") or {}
        evidence_status = observation.get("evidence_status", "partial")
        has_hypothesis = diagnosis.get("status") == "hypothesis"
        has_plan = observation.get("plan_candidate") is not None
        if mode == "investigate":
            status = "investigation_proposal" if has_hypothesis and has_plan else (
                "investigation_hypothesis" if has_hypothesis else "awaiting_evidence"
            )
        else:
            status = "shadow_hypothesis" if has_hypothesis else "awaiting_evidence"
        return {
            "status": status,
            "mode": mode,
            "incident_id": observation["incident_id"],
            "scenario_id": labels.get("scenario_id"),
            "stage": observation.get("stage"),
            "evidence_status": evidence_status,
            "evidence_refs": observation.get("evidence_refs", []),
            "decision_evidence_refs": observation.get("decision_evidence_refs", []),
            "evidence_sources": observation.get("evidence_sources", []),
            "collector_errors": observation.get("collector_errors", []),
            "diagnosis": diagnosis,
            "plan_candidate": observation.get("plan_candidate"),
            "runbook_context": observation.get("runbook_context", ""),
            "execution_permitted": False,
            "resolution_eligible": False,
        }

    reason = (
        "live execution is not connected to the evidence-derived investigator "
        "and safe executor; the existing ground-truth pipeline is not valid for "
        "live decisions; use investigate mode for read-only proposals"
        if mode == "live"
        else f"Moodle alert escalated because unified-core mode {mode!r} is not enabled; legacy fallback is disabled"
    )
    return {
        "status": "escalated",
        "scenario_id": labels.get("scenario_id"),
        "reason": reason,
        "execution_permitted": False,
        "resolution_eligible": False,
    }
