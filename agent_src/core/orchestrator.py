"""Checkpointed, deterministic orchestrator with full pipeline: OBSERVE → VERIFY."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from core.agents import DiagnosisAgent, PlannerAgent, UnsupportedRemediationError
from core.contract_validation import validate_pipeline_references
from core.dependency_graph import DependencyGraph
from core.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.evidence_store import EvidenceConflictError
from core.execution_agent import ActionResult, ExecutionAgent
from core.mvp_pipeline import evaluate_safety
from core.replay import ReplayObservation
from core.schema.action import TypedAction
from core.schema.audit import AuditEvent, StageAuditEvent
from core.schema.common import ActionDecision, IncidentStatus
from core.schema.diagnosis import DiagnosisResult
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource
from core.schema.safety import SafetyDecision
from core.schema.scenario import ScenarioGroundTruth
from core.schema.verification import VerificationResult
from core.sanitization import sanitize_log_excerpt
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
    input_digest: str = ""
    incident: Incident
    stages: list[OrchestratorStage] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    diagnosis: DiagnosisResult | None = None
    action: TypedAction | None = None
    safety: SafetyDecision | None = None
    execution_result: ActionResult | None = None
    verification: VerificationResult | None = None
    audit_events: list[AuditEvent | StageAuditEvent] = Field(default_factory=list)
    simulated: bool = True
    error: str | None = None

    @property
    def audit_completeness(self) -> float:
        expected = [stage.value for stage in self.stages]
        recorded = [
            event.stage
            for event in self.audit_events
            if isinstance(event, StageAuditEvent)
        ]
        if not expected:
            return 1.0
        return sum(
            1 for index, stage in enumerate(expected)
            if index < len(recorded) and recorded[index] == stage
        ) / len(expected)

    @property
    def audit_complete(self) -> bool:
        expected = [stage.value for stage in self.stages]
        recorded = [
            event.stage
            for event in self.audit_events
            if isinstance(event, StageAuditEvent)
        ]
        return recorded == expected


def _model_json_dict(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _input_digest(
    scenario: ScenarioGroundTruth,
    resources: list[Resource],
    observations: list[ReplayObservation],
) -> str:
    """Bind a checkpointed run ID to the exact inputs used to produce it."""
    payload = {
        "scenario": _model_json_dict(scenario),
        "resources": [_model_json_dict(resource) for resource in resources],
        "observations": [_model_json_dict(item) for item in observations],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


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
        input_digest = _input_digest(scenario, resources, observations)
        owner_token = uuid.uuid4().hex
        if not self.store.claim_checkpoint_run(run_id=run_id, owner_token=owner_token):
            raise EvidenceConflictError(f"checkpoint run is already owned: {run_id}")
        try:
            return self._run_claimed_scenario(
                scenario,
                resources,
                observations=observations,
                run_id=run_id,
                stop_after=stop_after,
                owner_token=owner_token,
                input_digest=input_digest,
            )
        finally:
            self.store.release_checkpoint_run(run_id=run_id, owner_token=owner_token)

    def _run_claimed_scenario(
        self,
        scenario: ScenarioGroundTruth,
        resources: list[Resource],
        *,
        observations: list[ReplayObservation],
        run_id: str,
        stop_after: OrchestratorStage | None,
        owner_token: str,
        input_digest: str,
    ) -> OrchestrationResult:
        latest = self.store.load_latest_checkpoint(run_id)
        if latest:
            result = OrchestrationResult(**latest["payload"])
            if result.input_digest != input_digest:
                raise EvidenceConflictError(
                    f"checkpoint run ID was reused with different inputs: {run_id}"
                )
            if result.stages and result.stages[-1] == OrchestratorStage.FAILED:
                return result
        else:
            result = self._observe(scenario, run_id, observations, input_digest)
            self._save(result, owner_token)
            if stop_after == OrchestratorStage.OBSERVE:
                return result

        graph = DependencyGraph(resources)
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
                self._save(result, owner_token)
                if stop_after == OrchestratorStage.TRIAGE:
                    return result

            if result.stages[-1] == OrchestratorStage.TRIAGE:
                result.diagnosis = self.diagnosis_agent.diagnose(
                    result.incident,
                    result.evidence,
                    graph,
                )
                if not result.diagnosis.ready_for_planning:
                    event = self.machine.transition(
                        result.incident,
                        IncidentStatus.ESCALATED,
                        actor="diagnosis_agent",
                        reason="Evidence is insufficient for a safe remediation plan.",
                        evidence_refs=result.diagnosis.evidence_refs,
                    )
                    result.audit_events.append(event)
                    result.stages.append(OrchestratorStage.DIAGNOSE)
                    self._save(result, owner_token)
                    return result
                event = self.machine.transition(
                    result.incident,
                    IncidentStatus.PLANNED,
                    actor="diagnosis_agent",
                    reason="Evidence-backed deterministic diagnosis is ready for planning.",
                    evidence_refs=result.diagnosis.evidence_refs,
                )
                result.audit_events.append(event)
                result.stages.append(OrchestratorStage.DIAGNOSE)
                self._save(result, owner_token)
                if stop_after == OrchestratorStage.DIAGNOSE:
                    return result

            if result.stages[-1] == OrchestratorStage.DIAGNOSE:
                assert result.diagnosis is not None
                try:
                    result.action = self.planner_agent.plan(
                        result.incident,
                        result.diagnosis,
                        graph,
                        evidence_items=result.evidence,
                    )
                except UnsupportedRemediationError:
                    event = self.machine.transition(
                        result.incident,
                        IncidentStatus.ESCALATED,
                        actor="planner_agent",
                        reason="No reviewed catalog action supports the diagnosed cause.",
                        evidence_refs=result.diagnosis.evidence_refs,
                    )
                    result.audit_events.append(event)
                    result.stages.append(OrchestratorStage.PLAN)
                    self._save(result, owner_token)
                    return result
                validate_pipeline_references(
                    result.incident,
                    result.action,
                    result.evidence,
                    resources,
                )
                result.stages.append(OrchestratorStage.PLAN)
                self._save(result, owner_token)
                if stop_after == OrchestratorStage.PLAN:
                    return result

            if result.stages[-1] == OrchestratorStage.PLAN:
                assert result.action is not None
                assert result.diagnosis is not None
                result.safety = evaluate_safety(result.action, result.diagnosis)
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
                self._save(result, owner_token)
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
                    result.execution_result = ActionResult(
                        action_id=result.action.action_id,
                        success=False,
                        output="Safety policy denied the action; no adapter was invoked.",
                        dry_run=True,
                        error="pipeline_denied",
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
                self._save(result, owner_token)
                if stop_after == OrchestratorStage.EXECUTE:
                    return result
                if not result.execution_result.success:
                    return result

            # --- VERIFY stage ---
            if result.stages[-1] == OrchestratorStage.EXECUTE:
                assert result.execution_result is not None
                if not result.execution_result.success:
                    return result
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
                if (
                    result.verification.verdict == "resolved"
                    and result.verification.resolution_eligible
                ):
                    event = self.machine.resolve_verified(
                        result.incident,
                        result.verification,
                        reason="All probes passed, communication contract verified.",
                        evidence_refs=result.incident.evidence_refs,
                    )
                    result.audit_events.append(event)
                else:
                    raise ValueError(
                        f"Verification failed: {result.verification.verdict}"
                    )
                result.stages.append(OrchestratorStage.VERIFY)
                self._save(result, owner_token)
            return result
        except Exception as exc:  # noqa: BLE001 - stage boundary must fail closed
            self._fail(result, exc, owner_token)
            return result

    def _observe(
        self,
        scenario: ScenarioGroundTruth,
        run_id: str,
        observations: list[ReplayObservation],
        input_digest: str,
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
            observed_at = base_time + timedelta(seconds=index)
            evidence = Evidence(
                evidence_id=_stable_id("ev", run_id, str(index), observation.summary),
                incident_id=incident.incident_id,
                resource_id=observation.resource_id,
                source=observation.source,
                observed_at=observed_at,
                collected_at=observed_at,
                summary=observation.summary,
                raw_ref=observation.raw_ref,
                content_hash="pending",
                metadata=observation.metadata,
            )
            evidence = evidence.model_copy(
                update={"content_hash": evidence_content_hash(evidence)}
            )
            self.store.append(evidence)
            evidence_items.append(evidence)
        incident.evidence_refs = [item.evidence_id for item in evidence_items]
        return OrchestrationResult(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            input_digest=input_digest,
            incident=incident,
            stages=[OrchestratorStage.OBSERVE],
            evidence=evidence_items,
            simulated=True,
        )

    def _fail(self, result: OrchestrationResult, exc: Exception, owner_token: str) -> None:
        if result.incident.status not in {IncidentStatus.RESOLVED, IncidentStatus.FAILED}:
            event = self.machine.transition(
                result.incident,
                IncidentStatus.FAILED,
                actor="orchestrator",
                reason=f"Malformed stage output ({type(exc).__name__}).",
                evidence_refs=result.incident.evidence_refs,
            )
            result.audit_events.append(event)
        result.error = f"{type(exc).__name__}: {sanitize_log_excerpt(str(exc), max_chars=240)}"
        result.stages.append(OrchestratorStage.FAILED)
        self._save(result, owner_token)

    def _save(self, result: OrchestrationResult, owner_token: str) -> None:
        stage = result.stages[-1]
        self._append_stage_audit(result, stage)
        sequence = (
            SUCCESS_STAGE_ORDER.index(stage)
            if stage in SUCCESS_STAGE_ORDER
            else len(SUCCESS_STAGE_ORDER)
        )
        checkpoint = {
            "run_id": result.run_id,
            "incident_id": result.incident.incident_id,
            "stage": stage.value,
            "sequence_number": sequence,
            "payload": _model_json_dict(result),
            "updated_at": datetime.now(timezone.utc),
            "audit_events": [_model_json_dict(event) for event in result.audit_events],
        }
        terminal = stage == OrchestratorStage.FAILED or (
            stage == OrchestratorStage.VERIFY
            and result.incident.status in {IncidentStatus.RESOLVED, IncidentStatus.VERIFIED_DRY_RUN}
        )
        if terminal:
            self.store.complete_checkpoint_run(owner_token=owner_token, **checkpoint)
        else:
            self.store.save_claimed_checkpoint(owner_token=owner_token, **checkpoint)

    @staticmethod
    def _append_stage_audit(
        result: OrchestrationResult, stage: OrchestratorStage
    ) -> None:
        if any(
            isinstance(event, StageAuditEvent) and event.stage == stage.value
            for event in result.audit_events
        ):
            return
        stage_outputs: dict[OrchestratorStage, Any] = {
            OrchestratorStage.OBSERVE: result.evidence,
            OrchestratorStage.TRIAGE: result.incident,
            OrchestratorStage.DIAGNOSE: result.diagnosis,
            OrchestratorStage.PLAN: result.action,
            OrchestratorStage.GATE_REQUEST: result.safety,
            OrchestratorStage.EXECUTE: result.execution_result,
            OrchestratorStage.VERIFY: result.verification,
        }
        output = stage_outputs.get(stage)
        if isinstance(output, list):
            output_payload: Any = [_model_json_dict(item) for item in output]
        elif output is not None:
            output_payload = _model_json_dict(output)
        else:
            output_payload = None
        output_digest = None
        if output_payload is not None:
            canonical = json.dumps(
                output_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            output_digest = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

        actor = {
            OrchestratorStage.OBSERVE: "observer",
            OrchestratorStage.TRIAGE: "observer",
            OrchestratorStage.DIAGNOSE: "diagnosis_agent",
            OrchestratorStage.PLAN: "planner_agent",
            OrchestratorStage.GATE_REQUEST: "safety_gate",
            OrchestratorStage.EXECUTE: "execution_agent",
            OrchestratorStage.VERIFY: "independent_verifier",
            OrchestratorStage.FAILED: "orchestrator",
        }[stage]
        outcome = "completed"
        details: dict[str, str] = {}
        if stage == OrchestratorStage.FAILED:
            outcome = "failed"
            details["error_type"] = result.error.split(":", 1)[0] if result.error else "UnknownError"
        elif (
            stage == OrchestratorStage.EXECUTE
            and result.execution_result is not None
            and result.execution_result.error
            and result.execution_result.error.startswith("pipeline_paused:")
        ):
            outcome = "awaiting_approval"
            details["approval_state"] = result.execution_result.error.split(":", 1)[1]
        elif (
            stage == OrchestratorStage.EXECUTE
            and result.execution_result is not None
            and result.execution_result.error == "pipeline_denied"
        ):
            outcome = "denied"
        if stage == OrchestratorStage.GATE_REQUEST and result.safety is not None:
            details["decision"] = result.safety.decision.value

        result.audit_events.append(
            StageAuditEvent(
                event_id=uuid.uuid4().hex,
                incident_id=result.incident.incident_id,
                run_id=result.run_id,
                stage=stage.value,
                outcome=outcome,
                actor=actor,
                evidence_refs=list(result.incident.evidence_refs),
                action_id=result.action.action_id if result.action else None,
                output_digest=output_digest,
                details=details,
            )
        )
