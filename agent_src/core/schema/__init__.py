"""Schemas for the AIOps control-plane contracts."""

from core.schema.common import (
    Environment, ResourceType, IncidentStatus, ActionDecision,
    ActionStatus, ActionType, EvidenceType,
)
from core.schema.resource import Resource
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.diagnosis import RootCauseHypothesis, DiagnosisResult
from core.schema.action import RollbackPlan, TypedAction
from core.schema.safety import SafetyDecision
from core.schema.verification import ProbeResult, VerificationResult
from core.schema.scenario import ScenarioGroundTruth, CommunicationContract
from core.schema.audit import AuditEvent

from core.schema.contract_validation import (
    ContractValidationError,
    validate_pipeline_references,
)
from core.schema.event_schema import (
    is_valid_normalized_event,
    normalize_alert,
    normalize_alertmanager_payload,
    validate_alertmanager_payload,
    validate_normalized_event,
)

SCHEMA_VERSION = "2.1"

__all__ = [
    "Environment", "ResourceType", "IncidentStatus", "ActionDecision",
    "ActionStatus", "ActionType", "EvidenceType",
    "Resource", "Evidence", "Incident",
    "RootCauseHypothesis", "DiagnosisResult",
    "RollbackPlan", "TypedAction",
    "SafetyDecision",
    "ProbeResult", "VerificationResult",
    "ScenarioGroundTruth", "CommunicationContract",
    "AuditEvent",
    "ContractValidationError",
    "validate_pipeline_references",
    "is_valid_normalized_event",
    "normalize_alert",
    "normalize_alertmanager_payload",
    "validate_alertmanager_payload",
    "validate_normalized_event",
    "SCHEMA_VERSION",
]
