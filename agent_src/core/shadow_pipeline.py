"""Feature-gated shadow integration for the unified Sprint 4 core."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.schema.evidence import Evidence
from core.schema.resource import Resource

logger = logging.getLogger(__name__)

VALID_UNIFIED_CORE_MODES = {"off", "shadow"}
DEFAULT_EVIDENCE_DB_PATH = "/app/data/evidence.db"


def get_unified_core_mode(value: str | None = None) -> str:
    raw = (value if value is not None else os.getenv("AIOPS_UNIFIED_CORE_MODE", "off"))
    mode = raw.strip().lower()
    if mode not in VALID_UNIFIED_CORE_MODES:
        logger.warning("Invalid AIOPS_UNIFIED_CORE_MODE=%r; failing closed to off", raw)
        return "off"
    return mode


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_resources() -> list[Resource]:
    path = _repo_root() / "evaluation" / "resources" / "moodle_resource_inventory.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Resource(**item) for item in data["resources"]]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _resolve_resource_id(alert: dict[str, Any], resources: list[Resource]) -> str | None:
    labels = alert.get("labels") or {}
    known = {resource.resource_id for resource in resources}
    for candidate in (
        labels.get("resource_id"),
        labels.get("component"),
        labels.get("service"),
        labels.get("target"),
    ):
        if candidate and str(candidate) in known:
            return str(candidate)
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

    fingerprint = str(alert.get("fingerprint") or _stable_id("fp", str(labels)))
    incident_id = _stable_id("inc", fingerprint)
    signal = str(labels.get("signal") or labels.get("alertname") or "unknown")
    summary = str((alert.get("annotations") or {}).get("summary") or signal)
    evidence = Evidence(
        evidence_id=_stable_id("ev", fingerprint, signal, str(alert.get("startsAt") or "")),
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
    database_path = os.getenv("AIOPS_EVIDENCE_DB_PATH", DEFAULT_EVIDENCE_DB_PATH)
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with SQLiteEvidenceStore(database_path) as store:
        store.append(evidence)
    logger.info(
        "Unified-core shadow observed alert: incident_id=%s resource_id=%s simulated=false",
        incident_id,
        resource_id,
    )
    return {
        "status": "observed",
        "incident_id": incident_id,
        "stage": "observe",
        "simulated": False,
        "reason": "awaiting_independent_collectors",
    }
