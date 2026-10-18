"""Sequential Pipeline Orchestrator executing the complete AIOps workflow.

Execution Sequence:
  observer → diagnosis → planner → safety gate → executor → verifier

Invariants:
1. Strict Sequential Ordering: Each role executes in deterministic order.
2. End-to-End Correlation: incident_id and evidence_ids are preserved and propagated
   through EVERY message envelope across all stages.
3. Fail-Closed Safety Gate:
   - If Safety Gate returns DENY, execution halts immediately. Executor and Verifier
     are NEVER invoked.
   - If Safety Gate returns REQUIRE_APPROVAL without an approved GatePass, execution
     halts immediately. Executor and Verifier are NEVER invoked.
4. Independent Verification Gate:
   - Only Verifier can transition the incident to RESOLVED.
   - If Verifier returns NOT_RESTORED or INCONCLUSIVE, incident transitions to FAILED.
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.action_plan import BlastRadius
from evaluation.agent_replay.domain.approval import ApprovalRecord, ApprovalScope, ApprovalStore, HumanGate
from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.domain.dry_run import ReadOnlyTargetClient
from evaluation.agent_replay.domain.incident import Incident, IncidentStatus
from evaluation.agent_replay.domain.resource import Resource
from evaluation.agent_replay.domain.safety_gate import PolicyDecision
from evaluation.agent_replay.roles.contracts import BaseRoleMessage
from evaluation.agent_replay.roles.diagnosis import DiagnosisInput, DiagnosisOutput, DiagnosisRole
from evaluation.agent_replay.roles.executor import ExecutorInput, ExecutorOutput, ExecutorRole
from evaluation.agent_replay.roles.observer import ObserverInput, ObserverOutput, ObserverRole
from evaluation.agent_replay.roles.planner import PlannerInput, PlannerOutput, PlannerRole
from evaluation.agent_replay.roles.safety_gate import SafetyGateInput, SafetyGateOutput, SafetyGateRole
from evaluation.agent_replay.roles.verifier import VerifierInput, VerifierOutput, VerifierRole
from evaluation.agent_replay.verification.communication_contract import CommunicationContract
from evaluation.agent_replay.verification.independent_verifier import VerifierVerdict


class PipelineRunInput(BaseModel):
    """Input payload to trigger a full or partial sequential pipeline run."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    alert_payload: dict[str, Any] = Field(..., description="Alertmanager alert payload.")
    scenario_id: str = Field(default="generic_scenario", description="Scenario identifier.")
    resources: list[Resource] = Field(default_factory=list, description="Known infrastructure resources.")
    dry_run_client: ReadOnlyTargetClient = Field(..., description="Target client for dry-run inspection.")
    target_client: Any = Field(..., description="Target execution client.")
    approval_store: ApprovalStore | None = Field(default=None, description="Approval store.")
    approval_scope: ApprovalScope | None = Field(default=None, description="Approval scope if available.")
    approval_record: ApprovalRecord | None = Field(default=None, description="Pre-existing approval if approved.")
    contract: CommunicationContract | None = Field(default=None, description="Communication contract.")
    contract_path: str | None = Field(default=None, description="Path to contract YAML.")
    health_probe_fn: Any | None = Field(default=None)
    allowed_probe_fn: Any | None = Field(default=None)
    forbidden_probe_fn: Any | None = Field(default=None)
    scenario_probe_fn: Any | None = Field(default=None)
    human_gate: HumanGate | None = Field(default=None, description="Human gate notifier.")
    audit_logger: SafetyAuditLogger | None = Field(default=None, description="Safety audit logger.")
    n_consecutive_checks: int = Field(default=3, ge=1, description="Required consecutive stable rounds.")
    incident_id: str | None = Field(default=None, description="Optional override for incident identifier.")
    blast_radius: BlastRadius | None = Field(default=None, description="Optional override for action plan blast radius.")


class PipelineExecutionResult(BaseRoleMessage):
    """Consolidated outcome of the sequential orchestration run."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    stages_completed: list[str] = Field(
        default_factory=list,
        description="Chronological list of successfully completed stage names.",
    )
    halted: bool = Field(
        default=False,
        description="True if the pipeline was halted early (e.g. by Safety Gate).",
    )
    halted_at_stage: str | None = Field(
        default=None,
        description="Stage name where the pipeline halted, if halted.",
    )
    halt_reason: str | None = Field(
        default=None,
        description="Reason explanation for halting the pipeline.",
    )
    incident: Incident = Field(..., description="Final state of the Incident domain model.")
    observer_output: ObserverOutput | None = Field(default=None)
    diagnosis_output: DiagnosisOutput | None = Field(default=None)
    planner_output: PlannerOutput | None = Field(default=None)
    safety_gate_output: SafetyGateOutput | None = Field(default=None)
    executor_output: ExecutorOutput | None = Field(default=None)
    verifier_output: VerifierOutput | None = Field(default=None)


class SequentialPipelineOrchestrator:
    """Orchestrates the 6-stage lifecycle sequentially while strictly enforcing safety boundaries."""

    def __init__(
        self,
        observer: ObserverRole | None = None,
        diagnosis: DiagnosisRole | None = None,
        planner: PlannerRole | None = None,
        safety_gate: SafetyGateRole | None = None,
        executor: ExecutorRole | None = None,
        verifier: VerifierRole | None = None,
    ) -> None:
        self.observer = observer or ObserverRole()
        self.diagnosis = diagnosis or DiagnosisRole()
        self.planner = planner or PlannerRole()
        self.safety_gate = safety_gate or SafetyGateRole()
        self.executor = executor or ExecutorRole()
        self.verifier = verifier or VerifierRole()

    def run(self, input_data: PipelineRunInput) -> PipelineExecutionResult:
        stages_completed: list[str] = []

        # =====================================================================
        # STAGE 1: OBSERVER
        # =====================================================================
        observer_out = self.observer.observe(
            ObserverInput(
                alert_payload=input_data.alert_payload,
                resources=input_data.resources,
                incident_id=input_data.incident_id,
            )
        )
        incident_id = observer_out.incident_id
        evidence_ids = list(observer_out.evidence_ids)
        incident = observer_out.incident
        stages_completed.append("observer")

        # =====================================================================
        # STAGE 2: DIAGNOSIS
        # =====================================================================
        diagnosis_out = self.diagnosis.diagnose(
            DiagnosisInput(
                incident_id=incident_id,
                evidence_ids=evidence_ids,
                incident=incident,
                evidence_items=observer_out.evidence_items,
                signals=observer_out.signals,
            )
        )
        stages_completed.append("diagnosis")
        evidence_ids = sorted(list(set(evidence_ids + list(diagnosis_out.evidence_ids))))

        if not diagnosis_out.ready_for_planning:
            incident.status = IncidentStatus.ESCALATED
            return PipelineExecutionResult(
                incident_id=incident_id,
                evidence_ids=evidence_ids,
                stages_completed=stages_completed,
                halted=True,
                halted_at_stage="diagnosis",
                halt_reason=f"Diagnosis unconfident: {diagnosis_out.reasoning_summary}",
                incident=incident,
                observer_output=observer_out,
                diagnosis_output=diagnosis_out,
            )

        # =====================================================================
        # STAGE 3: PLANNER
        # =====================================================================
        planner_kwargs: dict[str, Any] = {
            "incident_id": incident_id,
            "evidence_ids": evidence_ids,
            "incident": incident,
            "diagnosis": diagnosis_out,
            "evidence_items": observer_out.evidence_items,
        }
        if input_data.blast_radius is not None:
            planner_kwargs["blast_radius"] = input_data.blast_radius
        planner_out = self.planner.plan(PlannerInput(**planner_kwargs))

        stages_completed.append("planner")
        evidence_ids = sorted(list(set(evidence_ids + list(planner_out.evidence_ids))))
        incident.status = IncidentStatus.PLANNED

        # =====================================================================
        # STAGE 4: SAFETY GATE
        # =====================================================================
        approval_store = input_data.approval_store or ApprovalStore()
        safety_gate_out = self.safety_gate.evaluate(
            SafetyGateInput(
                incident_id=incident_id,
                evidence_ids=evidence_ids,
                plan=planner_out.plan,
                dry_run_client=input_data.dry_run_client,
                approval_store=approval_store,
                approval_scope=input_data.approval_scope,
                actor="pipeline_orchestrator",
                human_gate=input_data.human_gate,
                audit_logger=input_data.audit_logger,
            )
        )
        stages_completed.append("safety_gate")
        evidence_ids = sorted(list(set(evidence_ids + list(safety_gate_out.evidence_ids))))

        # FAIL-CLOSED CHECK:
        # If gate denied or requires approval without a valid GatePass -> HALT IMMEDIATELY!
        if safety_gate_out.gate_pass is None:
            if safety_gate_out.decision == PolicyDecision.REQUIRE_APPROVAL:
                incident.status = IncidentStatus.GATED
                halt_reason = f"Safety Gate REQUIRE_APPROVAL: {safety_gate_out.reason_code}"
            else:
                incident.status = IncidentStatus.FAILED
                halt_reason = f"Safety Gate DENY: {safety_gate_out.reason_code}"

            return PipelineExecutionResult(
                incident_id=incident_id,
                evidence_ids=evidence_ids,
                stages_completed=stages_completed,
                halted=True,
                halted_at_stage="safety_gate",
                halt_reason=halt_reason,
                incident=incident,
                observer_output=observer_out,
                diagnosis_output=diagnosis_out,
                planner_output=planner_out,
                safety_gate_output=safety_gate_out,
            )

        # =====================================================================
        # STAGE 5: EXECUTOR
        # =====================================================================
        executor_out = self.executor.execute(
            ExecutorInput(
                incident_id=incident_id,
                evidence_ids=evidence_ids,
                plan=planner_out.plan,
                gate_pass=safety_gate_out.gate_pass,
                target_client=input_data.target_client,
                approval_record=input_data.approval_record,
                audit_logger=input_data.audit_logger,
            )
        )
        stages_completed.append("executor")
        evidence_ids = sorted(list(set(evidence_ids + list(executor_out.evidence_ids))))
        incident.status = IncidentStatus.EXECUTED

        if not executor_out.success:
            incident.status = IncidentStatus.FAILED
            return PipelineExecutionResult(
                incident_id=incident_id,
                evidence_ids=evidence_ids,
                stages_completed=stages_completed,
                halted=True,
                halted_at_stage="executor",
                halt_reason=f"Executor failed: {executor_out.error}",
                incident=incident,
                observer_output=observer_out,
                diagnosis_output=diagnosis_out,
                planner_output=planner_out,
                safety_gate_output=safety_gate_out,
                executor_output=executor_out,
            )

        # =====================================================================
        # STAGE 6: VERIFIER
        # =====================================================================
        incident.status = IncidentStatus.VERIFYING
        verifier_out = self.verifier.verify(
            VerifierInput(
                incident_id=incident_id,
                evidence_ids=evidence_ids,
                scenario_id=input_data.scenario_id,
                plan_hash=planner_out.plan.plan_hash,
                contract=input_data.contract,
                contract_path=input_data.contract_path,
                n_consecutive_checks=input_data.n_consecutive_checks,
                health_probe_fn=input_data.health_probe_fn,
                allowed_probe_fn=input_data.allowed_probe_fn,
                forbidden_probe_fn=input_data.forbidden_probe_fn,
                scenario_probe_fn=input_data.scenario_probe_fn,
            )
        )
        stages_completed.append("verifier")
        evidence_ids = sorted(list(set(evidence_ids + list(verifier_out.evidence_ids))))

        # Update final status based exclusively on Independent Verifier
        if verifier_out.verdict == VerifierVerdict.RESTORED:
            incident.status = IncidentStatus.RESOLVED
        else:
            incident.status = IncidentStatus.FAILED

        return PipelineExecutionResult(
            incident_id=incident_id,
            evidence_ids=evidence_ids,
            stages_completed=stages_completed,
            halted=False,
            halted_at_stage=None,
            halt_reason=None,
            incident=incident,
            observer_output=observer_out,
            diagnosis_output=diagnosis_out,
            planner_output=planner_out,
            safety_gate_output=safety_gate_out,
            executor_output=executor_out,
            verifier_output=verifier_out,
        )
