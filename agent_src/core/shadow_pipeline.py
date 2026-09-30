"""Feature-gated shadow integration for the unified Sprint 4 core."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.evidence_collectors import collect_shadow_evidence, sanitize_log_excerpt
from core.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.schema.evidence import Evidence
from core.schema.resource import Resource

logger = logging.getLogger(__name__)

VALID_UNIFIED_CORE_MODES = {"off", "shadow"}
DEFAULT_EVIDENCE_DB_PATH = "/app/data/evidence.db"

# Stable mappings for the alert labels emitted by the Moodle monitoring
# playbook.  Alerts may omit ``resource_id`` because Prometheus expressions
# aggregate labels; the alert name is still a reviewed, finite contract.
MOODLE_ALERT_RESOURCE_MAP = {
    "MoodleNodeExporterDown": "moodle-app",
    "MoodleCAdvisorDown": "moodle-app",
    "MoodlePublicProbeFailed": "moodle-web-endpoint",
    "MoodleRdsTcpProbeFailed": "postgres-db",
    "MoodleEfsMountMissing": "moodledata-volume",
    "MoodleSyntheticTransactionFailed": "moodle-app",
    "MoodleSyntheticTransactionStale": "moodle-app",
    "MoodleNodeCpuHigh": "moodle-app",
    "MoodleWebContainerMissing": "moodle-app",
    "LogFileErrorDetected": "ai-agent",
}

RESOURCE_ALIASES = {
    "moodle": "moodle-app",
    "moodle-rds": "postgres-db",
    "moodle-efs": "moodledata-volume",
}


def get_unified_core_mode(value: str | None = None) -> str:
    raw = (value if value is not None else os.getenv("AIOPS_UNIFIED_CORE_MODE", "off"))
    mode = raw.strip().lower()
    if mode not in VALID_UNIFIED_CORE_MODES:
        logger.warning("Invalid AIOPS_UNIFIED_CORE_MODE=%r; failing closed to off", raw)
        return "off"
    return mode


def _load_resources() -> list[Resource]:
    path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "moodle_resource_inventory.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Resource(**item) for item in data["resources"]]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _resolve_resource_id(alert: dict[str, Any], resources: list[Resource]) -> str | None:
    labels = alert.get("labels") or {}
    known = {resource.resource_id for resource in resources}
    alert_resource = MOODLE_ALERT_RESOURCE_MAP.get(str(labels.get("alertname") or ""))
    if alert_resource in known:
        return alert_resource
    for candidate in (
        labels.get("resource_id"),
        labels.get("component"),
        labels.get("service"),
        labels.get("target"),
    ):
        normalized = RESOURCE_ALIASES.get(str(candidate), str(candidate))
        if candidate and normalized in known:
            return normalized
    return None


def run_shadow_if_enabled(alert: dict[str, Any]) -> dict[str, Any] | None:
    if get_unified_core_mode() != "shadow":
        return None
    resources = _load_resources()
    labels = alert.get("labels") or {}
    resource_id = _resolve_resource_id(alert, resources)
    if resource_id is None:
        logger.info("Unified-core shadow skipped: alert has no known resource mapping")
        return {"status": "skipped", "reason": "unknown_resource"}

    fingerprint = sanitize_log_excerpt(
        alert.get("fingerprint") or _stable_id("fp", str(labels)), max_chars=256
    )
    incident_id = _stable_id("inc", fingerprint)
    signal = sanitize_log_excerpt(
        labels.get("signal") or labels.get("alertname") or "unknown",
        max_chars=120,
    )
    summary = sanitize_log_excerpt(
        (alert.get("annotations") or {}).get("summary") or signal
    )
    evidence = Evidence(
        evidence_id=_stable_id(
            "ev", fingerprint, signal, str(alert.get("startsAt") or ""), summary
        ),
        incident_id=incident_id,
        resource_id=resource_id,
        source="alertmanager",
        collected_at=datetime.now(timezone.utc),
        summary=summary,
        raw_ref=f"alertmanager:{fingerprint}",
        content_hash="pending",
        metadata={
            "signal": signal,
            "status": str(alert.get("status") or "firing"),
            "simulated": "false",
        },
    )
    evidence.content_hash = evidence_content_hash(evidence)
    database_path = os.getenv(
        "AIOPS_EVIDENCE_DB_PATH",
        os.getenv("AIOPS_EVIDENCE_DB", DEFAULT_EVIDENCE_DB_PATH),
    )
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with SQLiteEvidenceStore(database_path) as store:
        store.append(evidence)
        collection = collect_shadow_evidence(
            alert,
            incident_id=incident_id,
            resource_id=resource_id,
            evidence_store=store,
            prometheus_url=os.getenv("PROMETHEUS_URL"),
        )
    evidence_refs = [evidence.evidence_id, *collection["evidence_refs"]]
    logger.info(
        "Unified-core shadow observed alert and collected context: incident_id=%s resource_id=%s sources=%s status=%s",
        incident_id,
        resource_id,
        collection["sources"],
        collection["status"],
    )
    return {
        "status": "observed",
        "incident_id": incident_id,
        "stage": "observe",
        "simulated": False,
        "reason": "awaiting_evidence-backed_diagnosis",
        "evidence_status": collection["status"],
        "evidence_refs": evidence_refs,
        "evidence_sources": collection["sources"],
        "collector_errors": collection["errors"],
        "execution_permitted": False,
        "resolution_eligible": False,
    }
