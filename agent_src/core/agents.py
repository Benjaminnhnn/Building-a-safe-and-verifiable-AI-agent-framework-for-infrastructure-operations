"""Deterministic Observer, Diagnosis, and Planner agents for offline/shadow use."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, ClassVar

from core.dependency_graph import DependencyGraph
from core.event_schema import (
    normalize_alertmanager_payload,
    validate_alertmanager_payload,
)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment, IncidentStatus
from core.schema.diagnosis import DiagnosisResult, RootCauseHypothesis
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource
from core.schema.scenario import ScenarioGroundTruth
from pydantic import BaseModel


class AgentContractError(ValueError):
    pass


class EvidenceRequest(BaseModel):
    resource_id: str
    source: str
    query_or_probe: str
    reason: str


class ObservationResult(BaseModel):
    incident: Incident
    normalized_events: list[dict[str, Any]]
    evidence_requests: list[EvidenceRequest]


@dataclass(frozen=True)
class DiagnosisRule:
    rule_id: str
    required_signals: frozenset[str]
    root_cause: str
    action_type: ActionType
    target_resource_id: str


# Operational rules are intentionally independent from evaluation ground truth.
# Ground-truth files may score these outputs, but agents never read expected RCA
# or allowed remediation fields while producing a diagnosis or action.
DIAGNOSIS_RULES: tuple[DiagnosisRule, ...] = (
    DiagnosisRule("postgres_stopped", frozenset({"postgres_exporter_up_zero", "moodle_database_connection_error", "synthetic_read_write_failed"}), "PostgreSQL container stopped", ActionType.START_CONTAINER, "postgres-db"),
    DiagnosisRule("postgres_connections_exhausted", frozenset({"postgres_connections_saturated", "moodle_database_wait_high", "synthetic_transaction_failed"}), "Available PostgreSQL connections are exhausted", ActionType.RESTART_CONTAINER, "postgres-db"),
    DiagnosisRule("invalid_database_endpoint", frozenset({"moodle_database_resolution_failed", "database_exporter_still_healthy", "synthetic_transaction_failed"}), "Moodle points to an invalid database endpoint", ActionType.RUN_ANSIBLE_PLAYBOOK, "moodle-app"),
    DiagnosisRule("moodle_stopped", frozenset({"moodle_endpoint_down", "container_state_exited", "postgres_exporter_still_up"}), "Moodle container stopped", ActionType.START_CONTAINER, "moodle-app"),
    DiagnosisRule("moodle_proxy_stopped", frozenset({"public_probe_failed", "proxy_container_absent", "moodle_internal_health_still_passes"}), "Moodle reverse proxy container is stopped", ActionType.START_CONTAINER, "moodle-proxy"),
    DiagnosisRule("moodle_crash_loop", frozenset({"container_restart_count_high", "moodle_health_failed", "database_exporter_still_healthy"}), "Controlled bad image configuration causes a crash loop", ActionType.RUN_ANSIBLE_PLAYBOOK, "moodle-app"),
    DiagnosisRule("database_dns_alias_loss", frozenset({"database_alias_resolution_failed", "postgres_exporter_still_healthy", "moodle_database_connection_failed"}), "Controlled database DNS alias is unavailable", ActionType.RUN_ANSIBLE_PLAYBOOK, "moodle-app"),
    DiagnosisRule("moodle_database_port_block", frozenset({"moodle_database_connection_timeout", "postgres_exporter_still_up", "blackbox_moodle_write_failed"}), "Scoped Moodle-to-PostgreSQL port block", ActionType.REMOVE_SCOPED_PORT_BLOCK, "postgres-db"),
    DiagnosisRule("database_path_degraded", frozenset({"database_round_trip_high", "moodle_request_latency_high", "synthetic_transaction_timeout"}), "Scoped fixture degrades the Moodle database path", ActionType.STOP_FAULT_INJECTOR, "moodle-app"),
    DiagnosisRule("moodle_cpu_pressure", frozenset({"host_cpu_saturation", "moodle_latency_high", "synthetic_transaction_timeout"}), "Controlled CPU hog on Moodle host", ActionType.STOP_FAULT_INJECTOR, "moodle-app"),
    DiagnosisRule("moodle_memory_pressure", frozenset({"host_memory_available_low", "moodle_latency_high", "container_memory_pressure"}), "Bounded fixture causes host memory pressure", ActionType.STOP_FAULT_INJECTOR, "moodle-app"),
    DiagnosisRule("moodle_disk_pressure", frozenset({"filesystem_free_space_low", "moodle_write_failed", "synthetic_transaction_failed"}), "Known bounded fixture consumes available disk space", ActionType.STOP_FAULT_INJECTOR, "moodledata-volume"),
    DiagnosisRule("database_port_exposed", frozenset({"forbidden_database_probe_succeeded", "database_service_still_healthy", "network_policy_drift_detected"}), "Controlled network policy exposes the database port", ActionType.RUN_ANSIBLE_PLAYBOOK, "postgres-db"),
    DiagnosisRule("moodledata_permission_drift", frozenset({"moodle_write_permission_denied", "synthetic_write_failed", "database_connection_still_healthy"}), "Moodle data directory ownership or mode is invalid", ActionType.RESTORE_MOODLEDATA_PERMISSION, "moodledata-volume"),
    DiagnosisRule("moodle_security_config_drift", frozenset({"moodle_redirect_contract_failed", "config_checksum_changed", "database_connection_still_healthy"}), "Moodle trusted proxy or site URL configuration drift", ActionType.RUN_ANSIBLE_PLAYBOOK, "moodle-app"),
)

DIAGNOSIS_RULES_BY_ID = {rule.rule_id: rule for rule in DIAGNOSIS_RULES}


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class ObserverAgent:
    def observe(self, payload: dict[str, Any], resources: list[Resource]) -> list[ObservationResult]:
        errors = validate_alertmanager_payload(payload)
        if errors:
            raise AgentContractError("; ".join(errors))

        normalized = normalize_alertmanager_payload(payload)
        resources_by_id = {resource.resource_id: resource for resource in resources}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in normalized["alerts"]:
            grouped[str(event["fingerprint"])].append(event)

        results: list[ObservationResult] = []
        for fingerprint in sorted(grouped):
            events = grouped[fingerprint]
            resource_ids = self._resolve_resources(events, resources_by_id)
            if not resource_ids:
                raise AgentContractError(
                    f"no known resource mapping for alert fingerprint {fingerprint}"
                )
            first = events[0]
            incident = Incident(
                incident_id=_stable_id("inc", fingerprint),
                fingerprint=fingerprint,
                title=str(first.get("signal", {}).get("name") or "Alertmanager incident"),
                status=IncidentStatus.OPEN,
                severity=str(first.get("severity", "warning")),
                affected_resources=resource_ids,
            )
            requests = [
                EvidenceRequest(
                    resource_id=resource_id,
                    source="prometheus",
                    query_or_probe=str(first.get("signal", {}).get("name") or "health"),
                    reason="Collect current metric/probe evidence for the grouped alert.",
                )
                for resource_id in resource_ids
            ]
            results.append(
                ObservationResult(
                    incident=incident,
                    normalized_events=events,
                    evidence_requests=requests,
                )
            )
        return results

    @staticmethod
    def _resolve_resources(
        events: list[dict[str, Any]],
        resources_by_id: dict[str, Resource],
    ) -> list[str]:
        candidates: set[str] = set()
        for event in events:
            labels = event.get("labels") or {}
            for value in (
                event.get("component_id"),
                event.get("service_id"),
                labels.get("target"),
                labels.get("resource_id"),
            ):
                if value and str(value) in resources_by_id:
                    candidates.add(str(value))
        return sorted(candidates)


class DiagnosisAgent:
    def diagnose(
        self,
        incident: Incident,
        evidence_items: list[Evidence],
        graph: DependencyGraph,
        scenarios: dict[str, ScenarioGroundTruth] | None = None,
    ) -> DiagnosisResult:
        generated_at = (
            max(item.collected_at for item in evidence_items)
            if evidence_items
            else incident.updated_at
        )
        for item in evidence_items:
            if item.incident_id != incident.incident_id:
                raise AgentContractError(
                    f"evidence {item.evidence_id} belongs to another incident"
                )
            if item.resource_id not in graph.resources:
                raise AgentContractError(
                    f"evidence {item.evidence_id} references unknown resource {item.resource_id}"
                )

        evidence_refs = sorted({item.evidence_id for item in evidence_items})
        missing: list[str] = []
        if len(evidence_refs) < 3:
            missing.append("at least three independent evidence records")
        observed_signals = {
            str(item.metadata.get("signal") or item.summary).strip().lower()
            for item in evidence_items
        }
        matching_rules = [
            rule for rule in DIAGNOSIS_RULES
            if rule.required_signals.issubset(observed_signals)
        ]
        if len(matching_rules) != 1:
            missing.append("an unambiguous operational diagnosis rule match")
        if missing:
            return DiagnosisResult(
                diagnosis_id=_stable_id("diag", incident.incident_id, "insufficient"),
                incident_id=incident.incident_id,
                generated_at=generated_at,
                evidence_refs=evidence_refs,
                missing_evidence=missing,
                confidence=0.0,
                ready_for_planning=False,
                notes="Insufficient deterministic evidence; no hypothesis was invented.",
            )

        rule = matching_rules[0]
        contradicting = sorted(
            item.evidence_id
            for item in evidence_items
            if item.metadata.get("contradicts", "false").lower() == "true"
        )
        supporting = sorted(set(evidence_refs) - set(contradicting))
        if not supporting:
            return DiagnosisResult(
                diagnosis_id=_stable_id("diag", incident.incident_id, rule.rule_id),
                incident_id=incident.incident_id,
                generated_at=generated_at,
                evidence_refs=evidence_refs,
                missing_evidence=["supporting evidence"],
                confidence=0.0,
                ready_for_planning=False,
            )

        confidence = 0.9 if not contradicting else 0.7
        hypothesis = RootCauseHypothesis(
            hypothesis_id=_stable_id("hyp", incident.incident_id, rule.rule_id),
            cause_code=rule.rule_id,
            root_cause=rule.root_cause,
            confidence=confidence,
            affected_resources=incident.affected_resources,
            supporting_evidence_refs=supporting,
            contradicting_evidence_refs=contradicting,
            reasoning_summary=(
                f"Operational rule {rule.rule_id} matched "
                f"{len(supporting)} supporting evidence records."
            ),
        )
        return DiagnosisResult(
            diagnosis_id=_stable_id("diag", incident.incident_id, rule.rule_id),
            incident_id=incident.incident_id,
            generated_at=generated_at,
            top_hypothesis=hypothesis,
            evidence_refs=evidence_refs,
            confidence=confidence,
            ready_for_planning=True,
        )


class PlannerAgent:
    FORBIDDEN_PARAMETER_KEYS: ClassVar[set[str]] = {
        "command",
        "raw_command",
        "shell",
        "script",
    }

    def plan(
        self,
        incident: Incident,
        diagnosis: DiagnosisResult,
        scenario: ScenarioGroundTruth,
        graph: DependencyGraph,
    ) -> TypedAction:
        if diagnosis.incident_id != incident.incident_id:
            raise AgentContractError("diagnosis incident_id does not match incident")
        if not diagnosis.ready_for_planning or diagnosis.top_hypothesis is None:
            raise AgentContractError("diagnosis is not ready for planning")
        if not diagnosis.evidence_refs:
            raise AgentContractError("planner requires evidence")
        cause_code = diagnosis.top_hypothesis.cause_code
        rule = DIAGNOSIS_RULES_BY_ID.get(cause_code or "")
        if rule is None:
            raise AgentContractError(f"no remediation policy for cause_code: {cause_code}")
        action_type = rule.action_type
        target = rule.target_resource_id
        if target not in graph.resources:
            raise AgentContractError(f"planner target does not exist: {target}")

        parameters = {
            "policy_rule": rule.rule_id,
            "adapter": "dry_run",
        }
        if self.FORBIDDEN_PARAMETER_KEYS.intersection(parameters):
            raise AgentContractError("raw command parameters are forbidden")

        rollback_available = True
        action = TypedAction(
            action_id=_stable_id("act", incident.incident_id, rule.rule_id, action_type.value),
            incident_id=incident.incident_id,
            action_type=action_type,
            target_resource_id=target,
            environment=Environment.STAGING,
            reason=diagnosis.top_hypothesis.reasoning_summary,
            evidence_refs=diagnosis.evidence_refs,
            parameters=parameters,
            preconditions=[
                "target resource exists",
                "environment is staging",
                "evidence references are valid",
            ],
            expected_outcome="Communication-contract probes and health checks can be verified.",
            reversible=rollback_available,
            rollback_plan=RollbackPlan(
                available=rollback_available,
                method="Run the typed inverse/reset operation and re-run independent probes.",
                expected_duration_seconds=60,
            ),
            requires_approval=False,
        )
        action.idempotency_key = _stable_id("idem", action.action_id)
        return action
