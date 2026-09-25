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
    "SCHEMA_VERSION",
]
