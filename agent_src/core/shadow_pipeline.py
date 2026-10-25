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

from core.evidence.evidence_collectors import collect_shadow_evidence
from core.safety.sanitization import sanitize_log_excerpt
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.platform_adapter import PlatformAdapter, PlatformAdapterRegistry
from core.reasoning.llm_diagnosis import (
    DiagnosisMode,
    get_diagnosis_mode,
    reason_about_alert,
    revalidate_cached_diagnosis,
)
from core.schema.evidence import Evidence
from core.schema.common import EvidenceType
from core.schema.resource import Resource

logger = logging.getLogger(__name__)

VALID_UNIFIED_CORE_MODES = {"off", "shadow", "investigate"}
DEFAULT_EVIDENCE_DB_PATH = "/app/data/evidence.db"


def get_unified_core_mode(value: str | None = None) -> str:
    raw = (value if value is not None else os.getenv("AIOPS_UNIFIED_CORE_MODE", "off"))
    mode = raw.strip().lower()
    if mode not in VALID_UNIFIED_CORE_MODES:
        logger.warning("Invalid AIOPS_UNIFIED_CORE_MODE=%r; failing closed to off", raw)
        return "off"
    return mode


def _load_resources(*, adapter: PlatformAdapter | None = None) -> list[Resource]:
    ad = adapter or PlatformAdapterRegistry.get("moodle")
    if ad:
        return ad.get_resources()
    return []


def _compute_dependency_impact(
    resource_id: str, adapter: PlatformAdapter
) -> tuple[list[str], list[str]]:
    confirmed = [resource_id]
    try:
        if hasattr(adapter, "get_dependency_graph"):
            graph = adapter.get_dependency_graph()
        else:
            from core.evidence.dependency_graph import DependencyGraph

            graph = DependencyGraph(adapter.get_resources())
        potentially_affected = graph.downstream(resource_id)
    except Exception:
        potentially_affected = []
    return confirmed, potentially_affected


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
    adapter: PlatformAdapter | None = None,
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

    ad = adapter or PlatformAdapterRegistry.get("moodle")
    if not ad:
        return None

    reasoning = diagnosis.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("source") not in (None, "rules"):
        # A cached LLM decision is re-checked by the validator, never re-asked.
        if not revalidate_cached_diagnosis(evidence, diagnosis):
            return None
        current_diagnosis = diagnosis
    else:
        current_diagnosis = ad.diagnose(alert_name, evidence)
        if (
            current_diagnosis.get("status") != "hypothesis"
            or current_diagnosis.get("cause_code") != diagnosis.get("cause_code")
            or current_diagnosis.get("hypothesis") != diagnosis.get("hypothesis")
        ):
            return None
    current_candidate = ad.plan_remediation(
        incident_id=incident_id,
        alert_name=alert_name,
        diagnosis=current_diagnosis,
        evidence=evidence,
    )
    if current_candidate != payload.get("remediation_candidate"):
        return None
    return payload


def _resolve_resource_id(
    alert: dict[str, Any],
    resources: list[Resource] | None = None,
    *,
    adapter: PlatformAdapter | None = None,
) -> str | None:
    ad = (
        adapter
        or PlatformAdapterRegistry.find_for_alert(alert)
        or PlatformAdapterRegistry.get("moodle")
    )
    if ad is not None:
        return ad.identify_resource(alert)
    return None


def _retrieve_runbook_with_provenance(
    alert: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Fetch alert-scoped authored runbook context and structured provenance."""
    if not os.getenv("VECTOR_DB_PATH"):
        return "", []
    labels = alert.get("labels") or {}
    alert_name = str(labels.get("alertname") or "")
    if not alert_name:
        return "", []
    from core.rag.rag_engine import get_rag_instance

    rag = get_rag_instance()
    if rag is None:
        return "", []
    summary = sanitize_log_excerpt(
        (alert.get("annotations") or {}).get("summary") or alert_name
    )
    if hasattr(rag, "retrieve_runbook_with_provenance"):
        data = rag.retrieve_runbook_with_provenance(summary, alert_name=alert_name)
        if isinstance(data, dict):
            return str(data.get("text") or ""), list(data.get("provenance") or [])
    text = rag.query_standard_runbooks(summary, alert_name=alert_name)
    provenance: list[dict[str, Any]] = []
    if text:
        import re

        for match in re.finditer(
            r"\[(?:Nguồn|Source):\s*([^;\]]+)(?:;\s*thời điểm:\s*([^;\]]+))?;\s*SHA-256:\s*([^\]]+)\]",
            text,
        ):
            provenance.append(
                {
                    "source_file": match.group(1).strip(),
                    "source_observed_at": (match.group(2) or "unknown").strip(),
                    "source_sha256": match.group(3).strip(),
                }
            )
    return text, provenance


def _retrieve_runbook_context(alert: dict[str, Any]) -> str:
    """Fetch alert-scoped authored runbook context, separate from decision inputs."""
    return _retrieve_runbook_with_provenance(alert)[0]


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


def run_shadow_if_enabled(
    alert: dict[str, Any], *, adapter: PlatformAdapter | None = None
) -> dict[str, Any] | None:
    """Collect evidence in shadow or read-only investigate mode; never execute."""
    if get_unified_core_mode() not in {"shadow", "investigate"}:
        return None
    effective_adapter = (
        adapter
        or PlatformAdapterRegistry.find_for_alert(alert)
        or PlatformAdapterRegistry.get("moodle")
    )
    if effective_adapter is None:
        logger.info("Unified-core shadow skipped: alert has no known adapter")
        return {"status": "skipped", "reason": "unknown_resource"}
    labels = alert.get("labels") or {}
    resource_id = effective_adapter.identify_resource(alert)
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
            adapter=effective_adapter,
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
                adapter=effective_adapter,
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
                queries=effective_adapter.get_alert_queries(
                    str(labels.get("alertname") or "unknown")
                ),
            )
            current_evidence_refs = [evidence.evidence_id, *collection["evidence_refs"]]
            evidence_items = [
                item
                for evidence_id in current_evidence_refs
                if (item := store.get_by_id(evidence_id)) is not None
            ]
            alert_name_str = str(labels.get("alertname") or "unknown")
            diagnosis = effective_adapter.diagnose(
                alert_name_str,
                evidence_items,
            )
            runbook_context, runbook_provenance = _retrieve_runbook_with_provenance(alert)
            diagnosis_mode = get_diagnosis_mode()
            if diagnosis_mode != DiagnosisMode.RULES and getattr(effective_adapter, "platform_name", None) == "moodle":
                diagnosis = reason_about_alert(
                    alert_name_str,
                    evidence_items,
                    rule_diagnosis=diagnosis,
                    mode=diagnosis_mode,
                    runbook_context=runbook_context,
                )
            plan_candidate = effective_adapter.plan_investigation(
                incident_id=incident_id,
                diagnosis=diagnosis,
            )
            remediation_candidate = None
            if get_unified_core_mode() == "investigate":
                remediation_candidate = effective_adapter.plan_remediation(
                    incident_id=incident_id,
                    alert_name=alert_name_str,
                    diagnosis=diagnosis,
                    evidence=evidence_items,
                )
            confirmed_affected_resources, potentially_affected_resources = (
                _compute_dependency_impact(resource_id, effective_adapter)
            )
            evidence_provenance = [
                {
                    "evidence_id": item.evidence_id,
                    "source": item.source,
                    "collected_at": item.collected_at.astimezone(timezone.utc).isoformat(),
                    "resource_id": item.resource_id,
                }
                for item in sorted(evidence_items, key=lambda e: e.evidence_id)
            ]
            supporting_refs_set = set(diagnosis.get("evidence_refs", []))
            supporting_provenance = [
                item
                for item in evidence_provenance
                if item["evidence_id"] in supporting_refs_set
            ]
            if hasattr(effective_adapter, "describe_remediation_disposition"):
                remediation_disposition = effective_adapter.describe_remediation_disposition(
                    alert_name=alert_name_str,
                    diagnosis=diagnosis,
                    remediation_candidate=remediation_candidate,
                )
            else:
                remediation_disposition = {
                    "disposition": (
                        "candidate_proposed"
                        if remediation_candidate is not None
                        else (
                            "escalated_awaiting_operator_isolation"
                            if diagnosis.get("status") == "hypothesis"
                            else "awaiting_valid_evidence"
                        )
                    ),
                    "reason": str(diagnosis.get("reason") or ""),
                    "execution_permitted": False,
                    "approval_required": bool(remediation_candidate),
                }
            explanation = {
                "status": str(diagnosis.get("status", "unknown")),
                "cause_code": diagnosis.get("cause_code"),
                "hypothesis": diagnosis.get("hypothesis"),
                "reason": diagnosis.get("reason"),
                "confidence": float(diagnosis.get("confidence", 0.0)),
                "confirmed_affected_resources": confirmed_affected_resources,
                "potentially_affected_resources": potentially_affected_resources,
                "supporting_evidence_provenance": supporting_provenance,
                "all_evidence_provenance": evidence_provenance,
                "runbook_provenance": runbook_provenance,
                "remediation_disposition": remediation_disposition,
            }
            diagnosis_record = {
                "status": str(diagnosis.get("status", "unknown")),
                "cause_code": diagnosis.get("cause_code"),
                "reason": sanitize_log_excerpt(diagnosis.get("reason", ""), max_chars=240),
                "hypothesis": sanitize_log_excerpt(diagnosis.get("hypothesis", ""), max_chars=240),
                "confidence": float(diagnosis.get("confidence", 0.0)),
                "evidence_refs": sorted(set(current_evidence_refs)),
                "supporting_evidence_refs": sorted(supporting_refs_set),
                "confirmed_affected_resources": confirmed_affected_resources,
                "potentially_affected_resources": potentially_affected_resources,
                "supporting_evidence_provenance": supporting_provenance,
                "runbook_provenance": runbook_provenance,
                "disposition": str(remediation_disposition.get("disposition", "unknown")),
            }
            reasoning = diagnosis.get("reasoning")
            if isinstance(reasoning, dict):
                llm_record = reasoning.get("llm") or {}
                diagnosis_record["reasoning"] = {
                    "mode": reasoning.get("mode"),
                    "source": reasoning.get("source"),
                    "rule_status": reasoning.get("rule_status"),
                    "rule_cause_code": reasoning.get("rule_cause_code"),
                    "llm_usage": llm_record.get("usage"),
                    "violation_codes": llm_record.get("violation_codes", []),
                    "selected": llm_record.get("selected"),
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
            remediation_candidate_evidence_id = None
            if remediation_candidate is not None:
                remediation_candidate_evidence_id = _persist_decision(
                    store,
                    incident_id=incident_id,
                    resource_id=resource_id,
                    kind="remediation_candidate",
                    payload=remediation_candidate,
                )
                decision_evidence_refs.append(remediation_candidate_evidence_id)
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
            result = {
                "status": "observed",
                "incident_id": incident_id,
                "stage": "diagnose" if diagnosis["status"] == "hypothesis" else "observe",
                "simulated": False,
                "reason": diagnosis["reason"],
                "diagnosis": diagnosis,
                "confirmed_affected_resources": confirmed_affected_resources,
                "potentially_affected_resources": potentially_affected_resources,
                "evidence_provenance": evidence_provenance,
                "runbook_provenance": runbook_provenance,
                "remediation_disposition": remediation_disposition,
                "explanation": explanation,
                "plan_candidate": plan_candidate.model_dump(mode="json") if plan_candidate else None,
                "remediation_candidate": remediation_candidate,
                "remediation_candidate_evidence_id": remediation_candidate_evidence_id,
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


def __getattr__(name: str) -> Any:
    if name in {"MOODLE_ALERT_RESOURCE_MAP", "RESOURCE_ALIASES"}:
        import importlib

        try:
            mod = importlib.import_module("core.moodle.adapter")
        except (ImportError, ModuleNotFoundError):
            mod = importlib.import_module("core.moodle_adapter")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
