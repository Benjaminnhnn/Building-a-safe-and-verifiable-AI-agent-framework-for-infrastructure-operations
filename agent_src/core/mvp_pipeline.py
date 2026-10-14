"""Evidence-derived offline replay pipeline for Moodle fixture observations.

Ground-truth labels are retained by the evaluation wrapper for scoring and
verification criteria. Diagnosis, planning, and policy receive only incident
context, evidence, dependency data, and the explicit action catalog.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path

from core.action_catalog import ActionCatalog
from core.agents import AgentContractError, DiagnosisAgent, PlannerAgent
from core.dependency_graph import DependencyGraph
from core.evidence_store import evidence_content_hash
from core.schema.action import TypedAction
from core.schema.common import ActionDecision, Environment, IncidentStatus
from core.schema.diagnosis import DiagnosisResult
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.safety import SafetyDecision
from core.schema.scenario import ScenarioGroundTruth
from core.schema.resource import Resource
from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult
from core.state_machine import IncidentStateMachine
from core.safety_policy_engine import BlastRadius, PolicyDecision, SafetyPolicyEngine

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
            severity="warning",
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
    graph = _load_resource_graph()
    diagnosis = diagnose(incident, evidence_items, graph)
    audit["timeline"]["t_diagnose"] = _now()

    if not diagnosis.ready_for_planning:
        machine = IncidentStateMachine()
        audit_events = [
            machine.transition(
                incident,
                IncidentStatus.TRIAGED,
                actor="observer",
                reason="Fixture observations were ingested for offline replay.",
                evidence_refs=incident.evidence_refs,
            ),
            machine.transition(
                incident,
                IncidentStatus.ESCALATED,
                actor="diagnosis_agent",
                reason="Diagnosis is insufficient; no action was planned or executed.",
                evidence_refs=diagnosis.evidence_refs,
            ),
        ]
        audit.update({
            "incident": incident,
            "evidence": evidence_items,
            "diagnosis": diagnosis,
            "action": None,
            "safety": None,
            "execution": {"dry_run": True, "executed": False, "action_id": None},
            "verification": None,
            "audit_events": audit_events,
        })
        return audit

    try:
        action = plan_action(incident, diagnosis, graph, evidence_items)
    except AgentContractError as exc:
        machine = IncidentStateMachine()
        audit_events = [
            machine.transition(
                incident,
                IncidentStatus.TRIAGED,
                actor="observer",
                reason="Fixture observations were ingested for offline replay.",
                evidence_refs=incident.evidence_refs,
            ),
            machine.transition(
                incident,
                IncidentStatus.ESCALATED,
                actor="planner_agent",
                reason="No catalog action matches this diagnosis and target class.",
                evidence_refs=diagnosis.evidence_refs,
            ),
        ]
        audit.update({
            "incident": incident,
            "evidence": evidence_items,
            "diagnosis": diagnosis,
            "action": None,
            "safety": None,
            "execution": {"dry_run": True, "executed": False, "action_id": None},
            "verification": None,
            "audit_events": audit_events,
            "planning_error": str(exc),
        })
        return audit
    audit["timeline"]["t_plan"] = _now()
    safety = evaluate_safety(action, diagnosis)
    audit["timeline"]["t_gate"] = _now()

    execution = {
        "dry_run": True,
        "executed": safety.decision == ActionDecision.ALLOW,
        "action_id": action.action_id,
    }
    if execution["executed"]:
        audit["timeline"]["t_execute_start"] = _now()
        audit["timeline"]["t_execute_end"] = _now()

    verification = None
    if safety.decision == ActionDecision.ALLOW and execution["executed"]:
        verification = verify_dry_run(incident, scenario)
        audit["timeline"]["t_verify"] = _now()
    machine = IncidentStateMachine()
    audit_events = []
    for next_state, actor in (
        (IncidentStatus.TRIAGED, "observer"),
        (IncidentStatus.PLANNED, "planner"),
        (IncidentStatus.GATED, "safety_gate"),
    ):
        audit_events.append(
            machine.transition(
                incident,
                next_state,
                actor=actor,
                reason="Moodle ground-truth dry-run stage completed.",
                evidence_refs=incident.evidence_refs,
            )
        )
    if verification is not None:
        audit_events.append(
            machine.transition(
                incident,
                IncidentStatus.EXECUTED,
                actor="executor",
                reason="Catalogued dry-run action was allowed by policy.",
                evidence_refs=incident.evidence_refs,
            )
        )
        audit_events.append(
            machine.transition(
                incident,
                IncidentStatus.VERIFYING,
                actor="independent_verifier",
                reason="Running explicit simulated recovery checks.",
                evidence_refs=incident.evidence_refs,
            )
        )
    if verification is not None and verification.resolution_eligible:
        audit_events.append(
            machine.resolve_verified(
                incident,
                verification,
                reason="Verifier probes, contract, and simulated stability window passed.",
                evidence_refs=incident.evidence_refs,
            )
        )
        audit["timeline"]["t_verified_dry_run"] = _now()

    audit.update(
        {
            "incident": incident,
            "evidence": evidence_items,
            "diagnosis": diagnosis,
            "action": action,
            "safety": safety,
            "execution": execution,
            "verification": verification,
            "audit_events": audit_events,
        }
    )
    return audit


def collect_fixture_evidence(scenario: ScenarioGroundTruth, incident: Incident) -> list[Evidence]:
    """Convert scenario signals into deterministic evidence records."""
    evidence: list[Evidence] = []
    signals = scenario.observed_signals[:]

    for idx, signal in enumerate(signals, start=1):
        resource_id = scenario.affected_resources[min(idx - 1, len(scenario.affected_resources) - 1)]
        summary = signal
        item = Evidence(
                evidence_id=_stable_id("ev", scenario.scenario_id, str(idx), signal),
                incident_id=incident.incident_id,
                resource_id=resource_id,
                source="evaluation_fixture",
                summary=summary,
                raw_ref=f"evaluation_fixture:{idx}",
                content_hash="pending",
                metadata={"signal": signal, "simulated": "true"},
            )
        evidence.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))
    return evidence


def diagnose(
    incident: Incident,
    evidence_items: list[Evidence],
    graph: DependencyGraph,
) -> DiagnosisResult:
    return DiagnosisAgent().diagnose(incident, evidence_items, graph)


def plan_action(
    incident: Incident,
    diagnosis: DiagnosisResult,
    graph: DependencyGraph,
    evidence_items: list[Evidence],
) -> TypedAction:
    return PlannerAgent().plan(
        incident,
        diagnosis,
        graph,
        evidence_items=evidence_items,
    )


def _load_resource_graph() -> DependencyGraph:
    path = Path("evaluation/resources/moodle_resource_inventory.json")
    inventory = json.loads(path.read_text(encoding="utf-8"))
    return DependencyGraph([Resource(**item) for item in inventory["resources"]])


def evaluate_safety(action: TypedAction, diagnosis: DiagnosisResult) -> SafetyDecision:
    """Evaluate planner output using policy and catalog data, never fixture labels."""
    catalog = ActionCatalog.load_moodle_catalog()
    catalog_action_id = action.parameters.get("catalog_action_id", "")
    entry = catalog.lookup(catalog_action_id)
    decision = PolicyDecision.DENY
    reasons: list[str] = []
    rollback_ready = False
    policy_role = "verifier" if entry is not None and entry.adapter == "probe" else "executor"

    if entry is None:
        reasons.append("action is not present in the Action Catalog")
    elif action.action_type != entry.action_type:
        reasons.append("typed action does not match the catalog action type")
    elif entry.permission.required_role == "operator":
        decision = PolicyDecision.HUMAN_ONLY
        reasons.append("catalogued action requires an operator and cannot be automated")
    elif not catalog.is_allowed(
        entry.action_id,
        environment=action.environment.value,
        role=policy_role,
    ):
        reasons.append("Action Catalog denies this role or environment")
    elif not action.evidence_refs or diagnosis.incident_id != action.incident_id:
        reasons.append("action is missing incident-bound diagnosis evidence")
    else:
        rollback_entry = catalog.lookup(entry.rollback_action or "")
        rollback_ready = bool(
            action.rollback_plan.available
            and action.reversible
            and action.rollback_plan.rollback_action_id == entry.rollback_action
            and entry.is_reversible
            and rollback_entry is not None
            and rollback_entry.rollback_action == entry.action_id
            and rollback_entry.adapter == entry.adapter
        )
        if entry.adapter != "probe" and entry.is_reversible and not rollback_ready:
            reasons.append("catalogued mutation requires a verified reciprocal rollback")
        elif entry.adapter != "probe" and not entry.is_reversible:
            reasons.append("non-reversible mutations are denied by default")
        elif entry.blast_radius not in BlastRadius.__members__:
            reasons.append("catalog entry has an unknown blast radius")
        else:
            policy = SafetyPolicyEngine.load_default_rules()
            decision, reason = policy.evaluate(
                action=entry.action_id,
                environment=action.environment.value,
                blast_radius=BlastRadius[entry.blast_radius],
                confidence=diagnosis.confidence,
                evidence_count=len(action.evidence_refs),
                rollback_ready=rollback_ready,
                actor_role=policy_role,
                is_reversible=rollback_ready if entry.adapter != "probe" else True,
            )
            reasons.append(reason)
            if (action.requires_approval or entry.default_requires_approval) and decision == PolicyDecision.ALLOW:
                decision = PolicyDecision.REQUIRE_APPROVAL
                reasons.append("typed action explicitly requires approval")

    mapped_decision = {
        PolicyDecision.ALLOW: ActionDecision.ALLOW,
        PolicyDecision.REQUIRE_APPROVAL: ActionDecision.REQUIRE_APPROVAL,
        PolicyDecision.DENY: ActionDecision.DENY,
        PolicyDecision.HUMAN_ONLY: ActionDecision.HUMAN_ONLY,
    }[decision]

    return SafetyDecision(
        decision_id=_stable_id("gate", action.action_id),
        action_id=action.action_id,
        action_hash=action.action_hash,
        incident_id=action.incident_id,
        decision=mapped_decision,
        reasons=reasons,
        required_evidence_refs=action.evidence_refs,
        approval_required=mapped_decision == ActionDecision.REQUIRE_APPROVAL,
        approval_ttl_seconds=600 if mapped_decision == ActionDecision.REQUIRE_APPROVAL else None,
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

    checked_at = datetime.now(timezone.utc)
    return VerificationResult(
        verification_id=_stable_id("verify", scenario.scenario_id),
        incident_id=incident.incident_id,
        health_passed=True,
        communication_contract_passed=True,
        stability_seconds=120,
        stability_observations=[
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=120), healthy=True
            ),
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=60), healthy=True
            ),
            StabilityObservation(observed_at=checked_at, healthy=True),
        ],
        resolution_eligible=True,
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
