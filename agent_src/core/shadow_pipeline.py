"""Feature-gated shadow integration for the unified Sprint 4 core."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.evidence_collectors import collect_shadow_evidence, sanitize_log_excerpt
from core.moodle_diagnosis import diagnose_moodle_alert
from core.moodle_planner import plan_moodle_investigation
from core.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.schema.evidence import Evidence
from core.schema.common import EvidenceType
from core.schema.resource import Resource

logger = logging.getLogger(__name__)

VALID_UNIFIED_CORE_MODES = {"off", "shadow", "investigate"}
DEFAULT_EVIDENCE_DB_PATH = "/app/data/evidence.db"

# Stable mappings for the alert labels emitted by the Moodle monitoring
# playbook.  Alerts may omit ``resource_id`` because Prometheus expressions
# aggregate labels; the alert name is still a reviewed, finite contract.
MOODLE_ALERT_RESOURCE_MAP = {
    "MoodleNodeExporterDown": "moodle-app",
    "MoodleCAdvisorDown": "moodle-app",
    "MoodlePublicProbeFailed": "moodle-web-endpoint",
    "MoodleRdsTcpProbeFailed": "postgres-db",
    "MoodleNodeRdsTcpFailed": "moodle-app",
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


def _alert_fingerprint(alert: dict[str, Any], labels: dict[str, Any]) -> str:
    fingerprint = alert.get("fingerprint")
    if not fingerprint:
        canonical_labels = json.dumps(
            labels, sort_keys=True, separators=(",", ":"), default=str
        )
        fingerprint = _stable_id("fp", canonical_labels)
    return sanitize_log_excerpt(fingerprint, max_chars=256)


def _fresh_cached_result(
    store: SQLiteEvidenceStore,
    cached: dict[str, Any] | None,
    *,
    incident_id: str,
    alert_name: str,
) -> dict[str, Any] | None:
    if not cached or cached.get("stage") != "shadow_complete":
        return None
    payload = cached.get("payload")
    if not (
        isinstance(payload, dict)
        and payload.get("status") == "observed"
        and payload.get("incident_id") == incident_id
    ):
        return None
    diagnosis = payload.get("diagnosis")
    if not isinstance(diagnosis, dict) or diagnosis.get("status") != "hypothesis":
        return None
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return None
    evidence = [store.get_by_id(ref) for ref in evidence_refs if isinstance(ref, str)]
    if len(evidence) != len(evidence_refs) or any(item is None for item in evidence):
        return None
    current_diagnosis = diagnose_moodle_alert(alert_name, evidence)
    if (
        current_diagnosis.get("status") != "hypothesis"
        or current_diagnosis.get("cause_code") != diagnosis.get("cause_code")
        or current_diagnosis.get("hypothesis") != diagnosis.get("hypothesis")
    ):
        return None
    return payload


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


def _retrieve_runbook_context(alert: dict[str, Any]) -> str:
    """Fetch alert-scoped authored runbook context, separate from decision inputs."""
    if not os.getenv("VECTOR_DB_PATH"):
        return ""
    labels = alert.get("labels") or {}
    alert_name = str(labels.get("alertname") or "")
    if not alert_name:
        return ""
    from core.rag_engine import get_rag_instance

    rag = get_rag_instance()
    if rag is None:
        return ""
    summary = sanitize_log_excerpt(
        (alert.get("annotations") or {}).get("summary") or alert_name
    )
    return rag.query_standard_runbooks(summary, alert_name=alert_name)


def _persist_decision(
    store: SQLiteEvidenceStore,
    *,
    incident_id: str,
    resource_id: str,
    kind: str,
    payload: dict[str, Any],
) -> str:
    """Append a hash-addressed, redacted decision record for audit/replay."""
    collected_at = datetime.now(timezone.utc)
    canonical_payload = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    evidence_id = _stable_id(
        "ev", incident_id, kind, collected_at.isoformat(), canonical_payload
    )
    summary = sanitize_log_excerpt(
        payload.get("hypothesis") or payload.get("reason") or kind,
        max_chars=240,
    )
    evidence = Evidence(
        evidence_id=evidence_id,
        incident_id=incident_id,
        resource_id=resource_id,
        evidence_type=EvidenceType.DECISION,
        source="ai_decision",
        collected_at=collected_at,
        summary=summary,
        raw_ref=None,
        content_hash="pending",
        confidence=float(payload.get("confidence", 1.0)),
        metadata={
            "decision_kind": kind,
            "decision_json": canonical_payload,
        },
    )
    evidence = evidence.model_copy(
        update={"content_hash": evidence_content_hash(evidence)}
    )
    store.append(evidence)
    return evidence_id


def run_shadow_if_enabled(alert: dict[str, Any]) -> dict[str, Any] | None:
    """Collect evidence in shadow or read-only investigate mode; never execute."""
    if get_unified_core_mode() not in {"shadow", "investigate"}:
        return None
    resources = _load_resources()
    labels = alert.get("labels") or {}
    resource_id = _resolve_resource_id(alert, resources)
    if resource_id is None:
        logger.info("Unified-core shadow skipped: alert has no known resource mapping")
        return {"status": "skipped", "reason": "unknown_resource"}

    fingerprint = _alert_fingerprint(alert, labels)
    incident_id = _stable_id(
        "inc", fingerprint, str(alert.get("startsAt") or "unknown-start")
    )
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
    evidence = evidence.model_copy(
        update={"content_hash": evidence_content_hash(evidence)}
    )
    database_path = os.getenv(
        "AIOPS_EVIDENCE_DB_PATH",
        os.getenv("AIOPS_EVIDENCE_DB", DEFAULT_EVIDENCE_DB_PATH),
    )
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with SQLiteEvidenceStore(database_path) as store:
        cached = store.load_latest_checkpoint(incident_id)
        cached_result = _fresh_cached_result(
            store,
            cached,
            incident_id=incident_id,
            alert_name=str(labels.get("alertname") or "unknown"),
        )
        if cached_result is not None:
            return cached_result

        owner_token = uuid.uuid4().hex
        if not store.claim_checkpoint_run(
            run_id=incident_id,
            owner_token=owner_token,
        ):
            cached = store.load_latest_checkpoint(incident_id)
            cached_result = _fresh_cached_result(
                store,
                cached,
                incident_id=incident_id,
                alert_name=str(labels.get("alertname") or "unknown"),
            )
            if cached_result is not None:
                return cached_result
            return {
                "status": "in_progress",
                "incident_id": incident_id,
                "execution_permitted": False,
                "resolution_eligible": False,
            }

        try:
            stored_alert = store.get_by_id(evidence.evidence_id)
            if stored_alert is None:
                store.append(evidence)
            else:
                evidence = stored_alert
            collection = collect_shadow_evidence(
                alert,
                incident_id=incident_id,
                resource_id=resource_id,
                evidence_store=store,
                prometheus_url=os.getenv("PROMETHEUS_URL"),
            )
            current_evidence_refs = [evidence.evidence_id, *collection["evidence_refs"]]
            diagnosis = diagnose_moodle_alert(
                str(labels.get("alertname") or "unknown"),
                [
                    item
                    for evidence_id in current_evidence_refs
                    if (item := store.get_by_id(evidence_id)) is not None
                ],
            )
            plan_candidate = plan_moodle_investigation(
                incident_id=incident_id,
                diagnosis=diagnosis,
            )
            diagnosis_record = {
                "status": str(diagnosis.get("status", "unknown")),
                "cause_code": diagnosis.get("cause_code"),
                "reason": sanitize_log_excerpt(diagnosis.get("reason", ""), max_chars=240),
                "hypothesis": sanitize_log_excerpt(diagnosis.get("hypothesis", ""), max_chars=240),
                "confidence": float(diagnosis.get("confidence", 0.0)),
                "evidence_refs": sorted(set(current_evidence_refs)),
                "supporting_evidence_refs": sorted(set(diagnosis.get("evidence_refs", []))),
            }
            if diagnosis.get("missing_queries"):
                diagnosis_record["missing_queries"] = [
                    sanitize_log_excerpt(query, max_chars=120)
                    for query in diagnosis["missing_queries"]
                ]
            if diagnosis.get("affected_nodes"):
                diagnosis_record["affected_nodes"] = [
                    sanitize_log_excerpt(node, max_chars=120)
                    for node in diagnosis["affected_nodes"]
                ]
            decision_evidence_refs = [
                _persist_decision(
                    store,
                    incident_id=incident_id,
                    resource_id=resource_id,
                    kind="diagnosis",
                    payload=diagnosis_record,
                )
            ]
            if plan_candidate is not None:
                decision_evidence_refs.append(
                    _persist_decision(
                        store,
                        incident_id=incident_id,
                        resource_id=resource_id,
                        kind="read_only_plan",
                        payload=plan_candidate.model_dump(mode="json"),
                    )
                )
            runbook_context = _retrieve_runbook_context(alert)
            result = {
                "status": "observed",
                "incident_id": incident_id,
                "stage": "diagnose" if diagnosis["status"] == "hypothesis" else "observe",
                "simulated": False,
                "reason": diagnosis["reason"],
                "diagnosis": diagnosis,
                "plan_candidate": plan_candidate.model_dump(mode="json") if plan_candidate else None,
                "decision_evidence_refs": decision_evidence_refs,
                "runbook_context": runbook_context,
                "evidence_status": collection["status"],
                "evidence_refs": current_evidence_refs,
                "evidence_sources": collection["sources"],
                "collector_errors": collection["errors"],
                "execution_permitted": False,
                "resolution_eligible": False,
            }
            store.complete_checkpoint_run(
                run_id=incident_id,
                owner_token=owner_token,
                incident_id=incident_id,
                stage="shadow_complete",
                sequence_number=1,
                payload=result,
                updated_at=datetime.now(timezone.utc),
            )
        except BaseException:
            store.release_checkpoint_run(run_id=incident_id, owner_token=owner_token)
            raise
    logger.info(
        "Unified-core shadow observed alert and collected context: incident_id=%s resource_id=%s sources=%s status=%s",
        incident_id,
        resource_id,
        collection["sources"],
        collection["status"],
    )
    return result
