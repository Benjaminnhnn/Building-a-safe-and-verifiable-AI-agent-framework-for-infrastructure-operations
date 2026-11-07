"""Correlate a resolved alert only with its exact audited CON-01 dispatch."""

from __future__ import annotations

import logging
import os
import sqlite3

from core.safety.safe_action_audit import SafeActionAuditStore
from core.shadow_pipeline import _alert_fingerprint, _stable_id

logger = logging.getLogger(__name__)


def con01_dispatch_for_alert(alert: dict) -> tuple[str, str] | None:
    labels = alert.get("labels") or {}
    if (labels.get("alertname") != "MoodleWebContainerMissing"
            or labels.get("environment") != "staging"
            or labels.get("instance") != "moodle-app-b"
            or not alert.get("fingerprint") or not alert.get("startsAt")):
        return None
    incident_id = _stable_id("inc", _alert_fingerprint(alert, labels), str(alert["startsAt"]))
    audit_db = os.getenv("AIOPS_SAFE_ACTION_AUDIT_DB", "/app/data/safe_action_audit.sqlite3")
    try:
        with SafeActionAuditStore(audit_db, read_only=True) as audit:
            if not audit.verify_chain(incident_id):
                return None
            events = audit.list_for_incident(incident_id)
            for event in reversed(events):
                payload = event.get("payload") or {}
                if (event.get("event_type") == "live_dispatch_started"
                        and payload.get("scenario_id") == "CON-01"
                        and payload.get("target_scope") == labels["instance"]
                        and isinstance(payload.get("request_id"), str)
                        and payload["request_id"]):
                    return incident_id, payload["request_id"]
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning("CON-01 alert correlation unavailable: %s", type(exc).__name__)
    return None


def verification_resolved(incident_id: str, request_id: str) -> bool:
    audit_db = os.getenv("AIOPS_SAFE_ACTION_AUDIT_DB", "/app/data/safe_action_audit.sqlite3")
    try:
        with SafeActionAuditStore(audit_db, read_only=True) as audit:
            return audit.get_live_incident_status(incident_id, request_id) == "resolved"
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning("CON-01 verification status unavailable: %s", type(exc).__name__)
        return False
