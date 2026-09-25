"""Checkpointed, deterministic orchestrator with full pipeline: OBSERVE → VERIFY."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from core.agents import DiagnosisAgent, PlannerAgent
from core.contract_validation import validate_pipeline_references
from core.dependency_graph import DependencyGraph
from core.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.execution_agent import ActionResult, ExecutionAgent
from core.mvp_pipeline import evaluate_safety
from core.replay import ReplayObservation
from core.schema.action import TypedAction
from core.schema.audit import AuditEvent
from core.schema.common import ActionDecision, IncidentStatus
from core.schema.diagnosis import DiagnosisResult
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource
from core.schema.safety import SafetyDecision
from core.schema.scenario import ScenarioGroundTruth
from core.schema.verification import VerificationResult
from core.state_machine import IncidentStateMachine, apply_gate_decision
from core.verification_agent import VerificationAgent
from pydantic import BaseModel, Field


class OrchestratorStage(str, Enum):
    OBSERVE = "observe"
    TRIAGE = "triage"
    DIAGNOSE = "diagnose"
    PLAN = "plan"
    GATE_REQUEST = "gate_request"
    EXECUTE = "execute"
    VERIFY = "verify"
    FAILED = "failed"


SUCCESS_STAGE_ORDER = [
    OrchestratorStage.OBSERVE,
    OrchestratorStage.TRIAGE,
    OrchestratorStage.DIAGNOSE,
    OrchestratorStage.PLAN,
    OrchestratorStage.GATE_REQUEST,
    OrchestratorStage.EXECUTE,
    OrchestratorStage.VERIFY,
]


class OrchestrationResult(BaseModel):
    run_id: str
    scenario_id: str
    incident: Incident
    stages: list[OrchestratorStage] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    diagnosis: DiagnosisResult | None = None
    action: TypedAction | None = None
    safety: SafetyDecision | None = None
    execution_result: ActionResult | None = None
    verification: VerificationResult | None = None
    audit_events: list[AuditEvent] = Field(default_factory=list)
    simulated: bool = True
    error: str | None = None


def _model_json_dict(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class CheckpointOrchestrator:
    def __init__(self, store: SQLiteEvidenceStore) -> None:
        self.store = store
        self.machine = IncidentStateMachine()
        self.diagnosis_agent = DiagnosisAgent()
        self.planner_agent = PlannerAgent()
        self.execution_agent = ExecutionAgent()
        self.verification_agent = VerificationAgent()

    def run_scenario(
        self,
        scenario: ScenarioGroundTruth,
        resources: list[Resource],
        *,
        observations: list[ReplayObservation],
        run_id: str | None = None,
        stop_after: OrchestratorStage | None = None,
    ) -> OrchestrationResult:
        run_id = run_id or _stable_id("run", scenario.scenario_id)
        latest = self.store.load_latest_checkpoint(run_id)
        if latest:
            result = OrchestrationResult(**latest["payload"])
            if result.stages and result.stages[-1] in {
                OrchestratorStage.GATE_REQUEST,
                OrchestratorStage.FAILED,
            }:
                return result
        else:
            result = self._observe(scenario, run_id, observations)
            self._save(result)
            if stop_after == OrchestratorStage.OBSERVE:
                return result

        graph = DependencyGraph(resources)
        scenario_catalog = {scenario.scenario_id: scenario}
        try:
            if result.stages[-1] == OrchestratorStage.OBSERVE:
                event = self.machine.transition(
                    result.incident,
                    IncidentStatus.TRIAGED,
                    actor="observer",
                    reason="Grouped alert fixture accepted for deterministic replay.",
                    evidence_refs=[item.evidence_id for item in result.evidence],
                )
                result.audit_events.append(event)
                result.stages.append(OrchestratorStage.TRIAGE)
                self._save(result)
                if stop_after == OrchestratorStage.TRIAGE:
                    return result

            if result.stages[-1] == OrchestratorStage.TRIAGE:
                result.diagnosis = self.diagnosis_agent.diagnose(
                    result.incident,
                    result.evidence,
                    graph,
                    scenario_catalog,
                )
                if not result.diagnosis.ready_for_planning:
                    raise ValueError("diagnosis is not ready for planning")
                event = self.machine.transition(
                    result.incident,
                    IncidentStatus.PLANNED,
                    actor="diagnosis_agent",
                    reason="Evidence-backed deterministic diagnosis is ready for planning.",
                    evidence_refs=result.diagnosis.evidence_refs,
                )
                result.audit_events.append(event)
                result.stages.append(OrchestratorStage.DIAGNOSE)
                self._save(result)
                if stop_after == OrchestratorStage.DIAGNOSE:
                    return result

            if result.stages[-1] == OrchestratorStage.DIAGNOSE:
                assert result.diagnosis is not None
                result.action = self.planner_agent.plan(
                    result.incident,
                    result.diagnosis,
                    scenario,
                    graph,
                )
                validate_pipeline_references(
                    result.incident,
                    result.action,
                    result.evidence,
                    resources,
                )
                result.stages.append(OrchestratorStage.PLAN)
                self._save(result)
                if stop_after == OrchestratorStage.PLAN:
                    return result

            if result.stages[-1] == OrchestratorStage.PLAN:
                assert result.action is not None
                result.safety = evaluate_safety(result.action, scenario)
                apply_gate_decision(result.action, result.safety.decision)
                event = self.machine.transition(
                    result.incident,
                    IncidentStatus.GATED,
                    actor="safety_gate",
                    reason=f"Gate request produced decision {result.safety.decision.value}.",
                    evidence_refs=result.action.evidence_refs,
                )
                result.audit_events.append(event)
                result.stages.append(OrchestratorStage.GATE_REQUEST)
                self._save(result)
                if stop_after == OrchestratorStage.GATE_REQUEST:
                    return result

            # --- EXECUTE stage ---
            if result.stages[-1] == OrchestratorStage.GATE_REQUEST:
                assert result.action is not None
                assert result.safety is not None
                if result.safety.decision == ActionDecision.ALLOW:
                    result.execution_result = self.execution_agent.execute(
                        result.action, result.safety, dry_run=True,
                    )
                    event = self.machine.transition(
                        result.incident,
                        IncidentStatus.EXECUTED,
                        actor="execution_agent",
                        reason=f"Action executed (dry_run={result.execution_result.dry_run}).",
                        evidence_refs=result.action.evidence_refs,
                    )
                    result.audit_events.append(event)
                elif result.safety.decision == ActionDecision.DENY:
                    raise ValueError(
                        f"Safety gate DENIED action: {'; '.join(result.safety.reasons)}"
                    )
                else:
                    # REQUIRE_APPROVAL or HUMAN_ONLY — stop pipeline, awaiting external input
                    result.execution_result = ActionResult(
                        action_id=result.action.action_id,
                        success=False,
                        output=f"Awaiting {result.safety.decision.value}",
                        dry_run=True,
                        error=f"pipeline_paused:{result.safety.decision.value}",
                    )
                result.stages.append(OrchestratorStage.EXECUTE)
                self._save(result)
                if stop_after == OrchestratorStage.EXECUTE:
                    return result

            # --- VERIFY stage ---
            if result.stages[-1] == OrchestratorStage.EXECUTE:
                assert result.execution_result is not None
                if result.execution_result.success:
                    result.verification = self.verification_agent.verify(
                        result.incident,
                        scenario.communication_contract,
                        dry_run=True,
                    )
                    event = self.machine.transition(
                        result.incident,
                        IncidentStatus.VERIFYING,
                        actor="independent_verifier",
                        reason="Running verification probes.",
                        evidence_refs=result.incident.evidence_refs,
                    )
                    result.audit_events.append(event)
                    if result.verification.verdict == "resolved":
                        event = self.machine.transition(
                            result.incident,
                            IncidentStatus.RESOLVED,
                            actor="independent_verifier",
                            reason="All probes passed, communication contract verified.",
                            evidence_refs=result.incident.evidence_refs,
                        )
                        result.audit_events.append(event)
                        result.incident.resolved_by_verifier = True
                    else:
                        raise ValueError(
                            f"Verification failed: {result.verification.verdict}"
                        )
                result.stages.append(OrchestratorStage.VERIFY)
                self._save(result)
            return result
        except Exception as exc:  # noqa: BLE001 - stage boundary must fail closed
            self._fail(result, exc)
            return result

    def _observe(
        self,
        scenario: ScenarioGroundTruth,
        run_id: str,
        observations: list[ReplayObservation],
    ) -> OrchestrationResult:
        if len(observations) < 3:
            raise ValueError("replay requires at least three externally supplied observations")
        incident = Incident(
            incident_id=_stable_id("inc", run_id, scenario.scenario_id),
            fingerprint=_stable_id("fp", scenario.scenario_id),
            title=scenario.scenario_name,
            severity=str((scenario.expected_impact or {}).get("severity", "warning")),
            affected_resources=scenario.affected_resources,
        )
        evidence_items: list[Evidence] = []
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index, observation in enumerate(observations, start=1):
            if observation.resource_id not in scenario.affected_resources:
                raise ValueError(
                    f"observation references unaffected resource: {observation.resource_id}"
                )
            evidence = Evidence(
                evidence_id=_stable_id("ev", run_id, str(index), observation.summary),
                incident_id=incident.incident_id,
                resource_id=observation.resource_id,
                source=observation.source,
                collected_at=base_time + timedelta(seconds=index),
                summary=observation.summary,
                raw_ref=observation.raw_ref,
                content_hash="pending",
                metadata=observation.metadata,
            )
            evidence.content_hash = evidence_content_hash(evidence)
            self.store.append(evidence)
            evidence_items.append(evidence)
        incident.evidence_refs = [item.evidence_id for item in evidence_items]
        return OrchestrationResult(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            incident=incident,
            stages=[OrchestratorStage.OBSERVE],
            evidence=evidence_items,
            simulated=True,
        )

    def _fail(self, result: OrchestrationResult, exc: Exception) -> None:
        if result.incident.status not in {IncidentStatus.RESOLVED, IncidentStatus.FAILED}:
            event = self.machine.transition(
                result.incident,
                IncidentStatus.FAILED,
                actor="orchestrator",
                reason=f"Malformed stage output: {exc}",
                evidence_refs=result.incident.evidence_refs,
            )
            result.audit_events.append(event)
        result.error = str(exc)
        result.stages.append(OrchestratorStage.FAILED)
        self._save(result)

    def _save(self, result: OrchestrationResult) -> None:
        stage = result.stages[-1]
        sequence = (
            SUCCESS_STAGE_ORDER.index(stage)
            if stage in SUCCESS_STAGE_ORDER
            else len(SUCCESS_STAGE_ORDER)
        )
        self.store.save_checkpoint(
            run_id=result.run_id,
            incident_id=result.incident.incident_id,
            stage=stage.value,
            sequence_number=sequence,
            payload=_model_json_dict(result),
            updated_at=datetime.now(timezone.utc),
        )
