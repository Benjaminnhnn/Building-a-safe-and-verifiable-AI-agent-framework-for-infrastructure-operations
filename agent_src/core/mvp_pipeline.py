"""Dry-run Sprint 2 pipeline for replaying Moodle ground-truth scenarios.

This module intentionally avoids live infrastructure calls. It lets the AI
Engineer validate the control-plane contract before the Infrastructure Engineer
finishes EC2, Compose, monitoring, injector, and reset wiring.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionDecision, ActionType, Environment, IncidentStatus
from core.schema.diagnosis import DiagnosisResult, RootCauseHypothesis
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.safety import SafetyDecision
from core.schema.scenario import ScenarioGroundTruth
from core.schema.verification import ProbeResult, VerificationResult

ACTION_MAP: dict[str, ActionType] = {
    "DB-01": ActionType.START_CONTAINER,
    "CON-01": ActionType.START_CONTAINER,
    "RES-01": ActionType.STOP_FAULT_INJECTOR,
    "NET-02": ActionType.REMOVE_SCOPED_PORT_BLOCK,
    "SEC-02": ActionType.RESTORE_MOODLEDATA_PERMISSION,
}


class IncidentCore:
    """Minimal in-memory incident core with fingerprint deduplication."""

    def __init__(self) -> None:
        self._by_fingerprint: dict[str, Incident] = {}

    def get_or_create(self, scenario: ScenarioGroundTruth) -> Incident:
        fingerprint = _stable_id("fingerprint", scenario.scenario_id, scenario.scenario_name)
        if fingerprint in self._by_fingerprint:
            return self._by_fingerprint[fingerprint]

        incident = Incident(
            incident_id=_stable_id("inc", scenario.scenario_id),
            fingerprint=fingerprint,
            title=scenario.scenario_name,
            status=IncidentStatus.OPEN,
            severity=str((scenario.expected_impact or {}).get("severity", "warning")),
            affected_resources=scenario.affected_resources,
        )
        self._by_fingerprint[fingerprint] = incident
        return incident


class EvidenceStore:
    """Append-only evidence store for replay tests."""

    def __init__(self) -> None:
        self._items: list[Evidence] = []

    def append(self, evidence: Evidence) -> None:
        if any(item.evidence_id == evidence.evidence_id for item in self._items):
            return
        self._items.append(evidence)

    def for_incident(self, incident_id: str) -> list[Evidence]:
        return [item for item in self._items if item.incident_id == incident_id]


def replay_scenario(data: dict[str, Any]) -> dict[str, Any]:
    """Run one ground-truth scenario through the dry-run vertical slice."""
    scenario = ScenarioGroundTruth(**data)
    incident_core = IncidentCore()
    evidence_store = EvidenceStore()

    audit: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "timeline": {
            "t_detect": _now(),
        },
    }

    incident = incident_core.get_or_create(scenario)
    audit["timeline"]["t_incident"] = _now()

    evidence_items = collect_fixture_evidence(scenario, incident)
    for evidence in evidence_items:
        evidence_store.append(evidence)

    incident.evidence_refs = [item.evidence_id for item in evidence_store.for_incident(incident.incident_id)]
    diagnosis = diagnose(incident, scenario, evidence_items)
    audit["timeline"]["t_plan"] = _now()

    action = plan_action(incident, scenario, diagnosis)
    safety = evaluate_safety(action, scenario)
    audit["timeline"]["t_gate"] = _now()

    audit["timeline"]["t_execute_start"] = _now()
    execution = {
        "dry_run": True,
        "executed": safety.decision == ActionDecision.ALLOW,
        "action_id": action.action_id,
    }
    audit["timeline"]["t_execute_end"] = _now()

    verification = verify_dry_run(incident, scenario)
    audit["timeline"]["t_verify"] = _now()
    if verification.verdict == "resolved":
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_by_verifier = True
        audit["timeline"]["t_resolved"] = _now()

    audit.update(
        {
            "incident": incident,
            "evidence": evidence_items,
            "diagnosis": diagnosis,
            "action": action,
            "safety": safety,
            "execution": execution,
            "verification": verification,
        }
    )
    return audit


def collect_fixture_evidence(scenario: ScenarioGroundTruth, incident: Incident) -> list[Evidence]:
    """Convert scenario signals into deterministic evidence records."""
    evidence: list[Evidence] = []
    signals = scenario.observed_signals[:]
    while len(signals) < 3:
        signals.append(f"{scenario.scenario_id.lower()}_supporting_signal_{len(signals) + 1}")

    for idx, signal in enumerate(signals, start=1):
        resource_id = scenario.affected_resources[min(idx - 1, len(scenario.affected_resources) - 1)]
        summary = f"{scenario.scenario_id} observed signal: {signal}"
        evidence.append(
            Evidence(
                evidence_id=_stable_id("ev", scenario.scenario_id, str(idx), signal),
                incident_id=incident.incident_id,
                resource_id=resource_id,
                source="fixture",
                summary=summary,
                raw_ref=f"ground_truth:{scenario.scenario_id}:{signal}",
                content_hash=_content_hash(summary),
                metadata={"scenario_id": scenario.scenario_id, "signal": signal},
            )
        )
    return evidence


def diagnose(
    incident: Incident,
    scenario: ScenarioGroundTruth,
    evidence_items: list[Evidence],
) -> DiagnosisResult:
    root_cause = str(scenario.expected_root_cause.get("cause", scenario.scenario_name))
    evidence_refs = [item.evidence_id for item in evidence_items]
    hypothesis = RootCauseHypothesis(
        hypothesis_id=_stable_id("hyp", scenario.scenario_id),
        root_cause=root_cause,
        confidence=0.9,
        affected_resources=scenario.affected_resources,
        supporting_evidence_refs=evidence_refs,
        reasoning_summary=f"{root_cause}; supported by {len(evidence_refs)} fixture evidence records.",
    )
    return DiagnosisResult(
        diagnosis_id=_stable_id("diag", scenario.scenario_id),
        incident_id=incident.incident_id,
        top_hypothesis=hypothesis,
        evidence_refs=evidence_refs,
        confidence=hypothesis.confidence,
        ready_for_planning=True,
    )


def plan_action(
    incident: Incident,
    scenario: ScenarioGroundTruth,
    diagnosis: DiagnosisResult,
) -> TypedAction:
    action_type = ACTION_MAP.get(scenario.scenario_id, ActionType.READ_HEALTH)
    target_resource_id = scenario.affected_resources[0]
    if scenario.scenario_id in {"DB-01", "NET-02"} and "postgres-db" in scenario.affected_resources:
        target_resource_id = "postgres-db"

    return TypedAction(
        action_id=_stable_id("act", scenario.scenario_id, action_type.value),
        incident_id=incident.incident_id,
        action_type=action_type,
        target_resource_id=target_resource_id,
        environment=Environment.STAGING,
        reason=diagnosis.top_hypothesis.reasoning_summary,
        evidence_refs=diagnosis.evidence_refs,
        parameters={"scenario_id": scenario.scenario_id, "dry_run": "true"},
        preconditions=["scenario fixture is valid", "target resource is in affected_resources"],
        expected_outcome="Verifier health, communication contract, and stability checks pass.",
        reversible=bool(scenario.rollback_plan.get("available", False)),
        rollback_plan=RollbackPlan(
            available=bool(scenario.rollback_plan.get("available", False)),
            method=str(scenario.rollback_plan.get("method", "")) or None,
            expected_duration_seconds=60,
        ),
        requires_approval=False,
    )


def evaluate_safety(action: TypedAction, scenario: ScenarioGroundTruth) -> SafetyDecision:
    reasons: list[str] = []
    deny_reasons: list[str] = []
    approval_reasons: list[str] = []

    if not action.evidence_refs:
        deny_reasons.append("action has no evidence")
    if action.action_type.value in scenario.forbidden_actions:
        deny_reasons.append("action is forbidden by ground truth")
    if action.environment != Environment.STAGING:
        approval_reasons.append("non-staging environment requires approval")
    if not action.rollback_plan.available:
        approval_reasons.append("rollback is not ready")

    if deny_reasons:
        decision = ActionDecision.DENY
        reasons.extend(deny_reasons)
        reasons.extend(approval_reasons)
    elif approval_reasons:
        decision = ActionDecision.REQUIRE_APPROVAL
        reasons.extend(approval_reasons)
    else:
        decision = ActionDecision.ALLOW

    if not reasons:
        reasons.append("staging dry-run action has evidence and rollback plan")

    return SafetyDecision(
        decision_id=_stable_id("gate", action.action_id),
        action_id=action.action_id,
        incident_id=action.incident_id,
        decision=decision,
        reasons=reasons,
        required_evidence_refs=action.evidence_refs,
        approval_required=decision == ActionDecision.REQUIRE_APPROVAL,
        approval_ttl_seconds=600 if decision == ActionDecision.REQUIRE_APPROVAL else None,
    )


def verify_dry_run(incident: Incident, scenario: ScenarioGroundTruth) -> VerificationResult:
    contract = scenario.communication_contract
    allowed = [
        ProbeResult(name=name, passed=True, details="dry-run allowed contract probe passed")
        for name in contract.get("allowed", [])
    ]
    forbidden = [
        ProbeResult(name=name, passed=True, details="dry-run forbidden contract probe remains blocked")
        for name in contract.get("forbidden", [])
    ]
    related = [
        ProbeResult(name=name, passed=True, details="dry-run related probe remained healthy")
        for name in contract.get("related", [])
    ]

    return VerificationResult(
        verification_id=_stable_id("verify", scenario.scenario_id),
        incident_id=incident.incident_id,
        health_passed=True,
        communication_contract_passed=True,
        stability_seconds=120,
        allowed_probes=allowed,
        forbidden_probes=forbidden,
        related_probes=related,
        verdict="resolved",
        simulated=True,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    joined = ":".join(parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
