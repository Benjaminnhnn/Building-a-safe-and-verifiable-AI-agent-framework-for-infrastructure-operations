"""Domain models for the AIOps control-plane.

These models are the typed, hash-stable contracts used by Observer →
Diagnosis → Planner → Safety Gate → Executor → Verifier.
"""

from evaluation.agent_replay.domain.enums import ActionType, EvidenceSource, Environment
from evaluation.agent_replay.domain.resource import Resource
from evaluation.agent_replay.domain.evidence import Evidence
from evaluation.agent_replay.domain.incident import Incident
from evaluation.agent_replay.domain.action_plan import (
    ActionPlan,
    BlastRadius,
    ExpectedScope,
    ReversibleRollback,
    IrreversibleRollback,
    Rollback,
)
from evaluation.agent_replay.domain.dry_run import (
    CallRecord,
    DictReadOnlyClient,
    DryRunDecision,
    DryRunPreview,
    DryRunResult,
    DryRunner,
    MutationNotAllowedError,
    ReadOnlyGuardClient,
    ReadOnlyTargetClient,
    StateDiff,
    evaluate_precondition,
)
from evaluation.agent_replay.domain.approval import (
    ApprovalRecord,
    ApprovalScope,
    ApprovalStore,
    HumanGate,
    UnauthorizedApproverError,
    TelegramApprovalNotifier,
)
from evaluation.agent_replay.domain.gate_pass import (
    GatePass,
)
from evaluation.agent_replay.domain.audit_log import (
    AuditEntry,
    SafetyAuditLogger,
)
from evaluation.agent_replay.domain.safety_gate import (
    PolicyDecision,
    SafetyGate,
    SafetyGateResult,
    format_diff_summary,
)
from evaluation.agent_replay.domain.executor import (
    ExecutionAbortedError,
    ExecutionResult,
    GatePassMissingError,
    SafeExecutor,
)

DOMAIN_VERSION = "2.0"

__all__ = [
    # enums
    "ActionType",
    "EvidenceSource",
    "Environment",
    # models
    "Resource",
    "Evidence",
    "Incident",
    "ActionPlan",
    # rollback union types
    "BlastRadius",
    "ExpectedScope",
    "ReversibleRollback",
    "IrreversibleRollback",
    "Rollback",
    # dry-run
    "CallRecord",
    "DictReadOnlyClient",
    "DryRunDecision",
    "DryRunPreview",
    "DryRunResult",
    "DryRunner",
    "MutationNotAllowedError",
    "ReadOnlyGuardClient",
    "ReadOnlyTargetClient",
    "StateDiff",
    "evaluate_precondition",
    # approval
    "ApprovalRecord",
    "ApprovalScope",
    "ApprovalStore",
    "HumanGate",
    "UnauthorizedApproverError",
    "TelegramApprovalNotifier",
    # gate pass
    "GatePass",
    # audit
    "AuditEntry",
    "SafetyAuditLogger",
    # safety gate
    "PolicyDecision",
    "SafetyGate",
    "SafetyGateResult",
    "format_diff_summary",
    # executor
    "ExecutionAbortedError",
    "ExecutionResult",
    "GatePassMissingError",
    "SafeExecutor",
    # version
    "DOMAIN_VERSION",
]
