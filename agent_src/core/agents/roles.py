"""Runtime role agents for the Moodle incident pipeline.

Alert path (``core.shadow_pipeline``): Observer -> Diagnosis -> Planner, then
a ``gate_request`` stage when a remediation candidate awaits approval.
Operator handoff (``core.main``): Execution wraps the Safety Gate and safe
executor; Verification accepts only a signed Independent Verifier result.

Agents never mutate infrastructure on the alert path, and only the
Verification agent can report a ``resolved`` status.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.agents.messages import (
    DiagnosisReport,
    ExecutionReport,
    GateRequest,
    ObservationReport,
    PlanProposal,
    VerificationReport,
    stage_event,
)
from core.evidence.evidence_store import SQLiteEvidenceStore
from core.platform_adapter import PlatformAdapter
from core.reasoning.llm_diagnosis import DiagnosisMode, reason_about_alert

logger = logging.getLogger(__name__)


class ObserverAgent:
    """Collects bounded read-only evidence for one alert."""

    name = "observer-agent"

    def __init__(self, adapter: PlatformAdapter, collect: Callable[..., dict[str, Any]], prometheus_url: str | None) -> None:
        self.adapter = adapter
        self.collect = collect
        self.prometheus_url = prometheus_url

    def run(
        self, *, alert: dict[str, Any], incident_id: str, resource_id: str, alert_evidence_id: str, store: Any
    ) -> tuple[ObservationReport, list[Any]]:
        alert_name = str((alert.get("labels") or {}).get("alertname") or "unknown")
        collection = self.collect(
            alert,
            incident_id=incident_id,
            resource_id=resource_id,
            evidence_store=store,
            prometheus_url=self.prometheus_url,
            queries=self.adapter.get_alert_queries(alert_name),
        )
        evidence_ids = [alert_evidence_id, *collection["evidence_refs"]]
        evidence_items = [item for evidence_id in evidence_ids if (item := store.get_by_id(evidence_id)) is not None]
        report = ObservationReport(
            incident_id=incident_id,
            run_id=incident_id,
            produced_by=self.name,
            evidence_ids=evidence_ids,
            alert_name=alert_name,
            resource_id=resource_id,
            alert_evidence_id=alert_evidence_id,
            evidence_status=str(collection["status"]),
            evidence_sources=list(collection["sources"]),
            collector_errors=list(collection["errors"]),
        )
        return report, evidence_items


class DiagnosisAgent:
    """Rule diagnosis, optionally refined by the evidence-cited LLM layer."""

    name = "diagnosis-agent"

    def __init__(
        self,
        adapter: PlatformAdapter,
        *,
        mode: DiagnosisMode,
        runbook_chunks: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.runbook_chunks = runbook_chunks

    def run(self, observation: ObservationReport, evidence: list[Any]) -> tuple[DiagnosisReport, list[dict[str, Any]]]:
        diagnosis = self.adapter.diagnose(observation.alert_name, evidence)
        chunks: list[dict[str, Any]] = []
        if self.mode != DiagnosisMode.RULES and getattr(self.adapter, "platform_name", None) == "moodle":
            chunks = self.runbook_chunks()
            diagnosis = reason_about_alert(
                observation.alert_name,
                evidence,
                rule_diagnosis=diagnosis,
                mode=self.mode,
                runbook_chunks=chunks,
            )
        reasoning = diagnosis.get("reasoning") or {}
        report = DiagnosisReport(
            incident_id=observation.incident_id,
            run_id=observation.run_id,
            produced_by=self.name,
            evidence_ids=sorted(set(diagnosis.get("evidence_refs") or [])),
            diagnosis=diagnosis,
            diagnosis_mode=self.mode.value,
            decided_by=str(reasoning.get("source", "rules")),
            runbook_chunk_ids=[chunk["chunk_id"] for chunk in chunks if chunk.get("chunk_id")],
        )
        return report, chunks


class PlannerAgent:
    """Maps a diagnosis to a catalogued read-only probe or exact-fault candidate."""

    name = "planner-agent"

    def __init__(
        self,
        adapter: PlatformAdapter,
        *,
        allow_remediation: bool,
        dependency_impact: Callable[[str, PlatformAdapter], tuple[list[str], list[str]]],
    ) -> None:
        self.adapter = adapter
        self.allow_remediation = allow_remediation
        self.dependency_impact = dependency_impact

    def run(
        self, observation: ObservationReport, diagnosis_report: DiagnosisReport, evidence: list[Any]
    ) -> PlanProposal:
        diagnosis = diagnosis_report.diagnosis
        plan = self.adapter.plan_investigation(incident_id=observation.incident_id, diagnosis=diagnosis)
        candidate = None
        if self.allow_remediation:
            candidate = self.adapter.plan_remediation(
                incident_id=observation.incident_id,
                alert_name=observation.alert_name,
                diagnosis=diagnosis,
                evidence=evidence,
            )
        confirmed, potentially = self.dependency_impact(observation.resource_id, self.adapter)
        if hasattr(self.adapter, "describe_remediation_disposition"):
            disposition = self.adapter.describe_remediation_disposition(
                alert_name=observation.alert_name,
                diagnosis=diagnosis,
                remediation_candidate=candidate,
            )
        else:
            disposition = {
                "disposition": (
                    "candidate_proposed"
                    if candidate is not None
                    else (
                        "escalated_awaiting_operator_isolation"
                        if diagnosis.get("status") == "hypothesis"
                        else "awaiting_valid_evidence"
                    )
                ),
                "reason": str(diagnosis.get("reason") or ""),
                "execution_permitted": False,
                "approval_required": bool(candidate),
            }
        action_id = None
        if candidate is not None:
            action_id = (candidate.get("action") or {}).get("action_id")
        elif plan is not None:
            action_id = plan.action_id
        return PlanProposal(
            incident_id=observation.incident_id,
            run_id=observation.run_id,
            produced_by=self.name,
            evidence_ids=sorted(set((candidate or {}).get("evidence_refs") or diagnosis_report.evidence_ids)),
            action_id=action_id,
            read_only_plan=plan.model_dump(mode="json") if plan is not None else None,
            remediation_candidate=candidate,
            disposition=disposition,
            confirmed_affected_resources=confirmed,
            potentially_affected_resources=potentially,
        )


def alert_path_stage_events(
    observation: ObservationReport, diagnosis: DiagnosisReport, plan: PlanProposal
) -> list[dict[str, Any]]:
    """Stage events for one alert-path run; the gate request waits for approval."""
    events = [
        stage_event(observation, outcome="completed", details={
            "evidence_status": observation.evidence_status,
            "sources": ",".join(observation.evidence_sources),
        }),
        stage_event(diagnosis, outcome="completed", details={
            "status": diagnosis.diagnosis.get("status"),
            "cause_code": diagnosis.diagnosis.get("cause_code"),
            "mode": diagnosis.diagnosis_mode,
            "decided_by": diagnosis.decided_by,
        }),
        stage_event(plan, outcome="completed", details={
            "disposition": plan.disposition.get("disposition"),
            "read_only_plan": "yes" if plan.read_only_plan else "no",
        }),
    ]
    if plan.remediation_candidate is not None:
        events.append(stage_event(
            plan.model_copy(update={"stage": "gate_request", "produced_by": "safety-gate"}),
            outcome="awaiting_approval",
            details={
                "scenario_id": plan.remediation_candidate.get("scenario_id"),
                "catalog_action_id": plan.remediation_candidate.get("catalog_action_id"),
            },
        ))
    return events


def handoff_run_id(incident_id: str) -> str:
    """Ledger run for operator-handoff stages.

    The alert-path ledger (run ``incident_id``) is embedded in its checkpoint
    and must not grow afterwards, so handoff stages chain under their own run.
    """
    return f"{incident_id}:handoff"


def _append_stage(evidence_db: str | Path | None, event: dict[str, Any]) -> bool:
    """Append a handoff stage event; the safe-action audit stays authoritative."""
    if not evidence_db:
        return False
    try:
        with SQLiteEvidenceStore(evidence_db) as store:
            store.append_audit_events(run_id=event["run_id"], events=[event])
        return True
    except Exception as exc:  # noqa: BLE001 - never mask the execution/verifier result
        logger.warning("Stage audit append failed for %s: %s", event.get("stage"), type(exc).__name__)
        return False


class ExecutionAgent:
    """Operator handoff: prepare the gate request, then execute with both signatures."""

    name = "execution-agent"

    def __init__(
        self,
        *,
        evidence_db: str | Path,
        prepare: Callable[..., dict[str, Any]],
        execute: Callable[..., dict[str, Any]],
    ) -> None:
        self.evidence_db = evidence_db
        self._prepare = prepare
        self._execute = execute

    def prepare(self, *, candidate_id: str, **kwargs: Any) -> dict[str, Any]:
        result = self._prepare(evidence_db=self.evidence_db, candidate_id=candidate_id, **kwargs)
        request = result.get("request") or {}
        action = request.get("action") or {}
        message = GateRequest(
            incident_id=str(result["incident_id"]),
            run_id=handoff_run_id(str(result["incident_id"])),
            produced_by="safety-gate",
            evidence_ids=list(action.get("evidence_refs") or []),
            action_id=action.get("action_id"),
            candidate_id=candidate_id,
            request_id=str(request.get("request_id", "")),
            gate_action_sha256=str(result.get("gate_action_sha256", "")),
            executor_action_sha256=str(result.get("executor_action_sha256", "")),
        )
        _append_stage(self.evidence_db, stage_event(message, outcome="awaiting_approval", details={
            "request_id": message.request_id,
            "gate_action_sha256": message.gate_action_sha256,
        }))
        return result

    def execute(self, *, candidate_id: str, request_data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = self._execute(
            evidence_db=self.evidence_db, candidate_id=candidate_id, request_data=request_data, **kwargs
        )
        action = request_data.get("action") or {}
        incident_id = str(action.get("incident_id") or "")
        if incident_id:
            status = str(result.get("status", "unknown"))
            message = ExecutionReport(
                incident_id=incident_id,
                run_id=handoff_run_id(incident_id),
                produced_by=self.name,
                evidence_ids=list(action.get("evidence_refs") or []),
                action_id=action.get("action_id"),
                candidate_id=candidate_id,
                request_id=str(request_data.get("request_id", "")),
                status=status,
                execution_permitted=bool(result.get("execution_permitted", False)),
            )
            outcome = {
                "awaiting_verification": "completed",
                "denied": "denied",
                "awaiting_approval": "awaiting_approval",
            }.get(status, "failed")
            _append_stage(self.evidence_db, stage_event(message, outcome=outcome, details={
                "status": status,
                "request_id": message.request_id,
            }))
        return result


class VerificationAgent:
    """Accepts a signed Independent Verifier attestation; the only path to RESOLVED."""

    name = "verification-agent"

    def __init__(self, *, evidence_db: str | Path | None, accept: Callable[..., dict[str, Any]]) -> None:
        self.evidence_db = evidence_db
        self._accept = accept

    def run(self, *, request_id: str, attestation: Any, **kwargs: Any) -> dict[str, Any]:
        result = self._accept(request_id=request_id, attestation=attestation, **kwargs)
        payload = attestation.payload
        status = str(result.get("status", "unknown"))
        message = VerificationReport(
            incident_id=payload.incident_id,
            run_id=handoff_run_id(payload.incident_id),
            produced_by=self.name,
            request_id=request_id,
            status=status,
            resolution_eligible=status == "resolved",
        )
        _append_stage(self.evidence_db, stage_event(
            message,
            outcome="completed" if status in {"resolved", "verifying"} else "failed",
            details={"status": status, "event_hash": result.get("event_hash")},
        ))
        return result
