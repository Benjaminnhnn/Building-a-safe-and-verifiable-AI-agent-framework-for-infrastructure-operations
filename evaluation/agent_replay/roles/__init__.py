"""Modular AIOps Roles and Sequential Orchestrator.

Modules:
- observer: Generates typed Incident & Evidence from alerts.
- diagnosis: Evaluates deterministic root cause hypotheses.
- planner: Proposes typed ActionPlan with ZERO execution authority.
- safety_gate: Validates dry-run, preconditions, policies, and human approvals.
- executor: Executes with mandatory GatePass.
- verifier: Independent probe verification across N consecutive stable rounds.
- orchestrator: Sequential pipeline coordinator.
"""

from __future__ import annotations

from evaluation.agent_replay.roles.contracts import BaseRoleMessage
from evaluation.agent_replay.roles.observer import ObserverInput, ObserverOutput, ObserverRole
from evaluation.agent_replay.roles.diagnosis import DiagnosisInput, DiagnosisOutput, DiagnosisRole
from evaluation.agent_replay.roles.planner import PlannerInput, PlannerOutput, PlannerRole
from evaluation.agent_replay.roles.safety_gate import SafetyGateInput, SafetyGateOutput, SafetyGateRole
from evaluation.agent_replay.roles.executor import ExecutorInput, ExecutorOutput, ExecutorRole
from evaluation.agent_replay.roles.verifier import VerifierInput, VerifierOutput, VerifierRole
from evaluation.agent_replay.roles.orchestrator import (
    PipelineRunInput,
    PipelineExecutionResult,
    SequentialPipelineOrchestrator,
)

__all__ = [
    # Contracts
    "BaseRoleMessage",
    # Observer
    "ObserverInput",
    "ObserverOutput",
    "ObserverRole",
    # Diagnosis
    "DiagnosisInput",
    "DiagnosisOutput",
    "DiagnosisRole",
    # Planner
    "PlannerInput",
    "PlannerOutput",
    "PlannerRole",
    # Safety Gate
    "SafetyGateInput",
    "SafetyGateOutput",
    "SafetyGateRole",
    # Executor
    "ExecutorInput",
    "ExecutorOutput",
    "ExecutorRole",
    # Verifier
    "VerifierInput",
    "VerifierOutput",
    "VerifierRole",
    # Orchestrator
    "PipelineRunInput",
    "PipelineExecutionResult",
    "SequentialPipelineOrchestrator",
]

