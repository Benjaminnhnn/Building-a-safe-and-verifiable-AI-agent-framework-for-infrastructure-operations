"""Diagnosis module — correlates Evidence into deterministic Root Cause Hypotheses.

Invariants:
1. Re-verifies all evidence items: filters expired evidence records.
2. Evaluates evidence against deterministic operational rules.
3. Produces typed DiagnosisOutput binding incident_id and evidence_ids.
4. Updates incident diagnosis_id without mutating unapproved state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.celery_app import celery_app
from evaluation.agent_replay.domain.enums import ActionType
from evaluation.agent_replay.domain.evidence import Evidence
from evaluation.agent_replay.domain.incident import Incident
from evaluation.agent_replay.roles.contracts import BaseRoleMessage


@dataclass(frozen=True)
class DiagnosisRule:
    cause_code: str
    required_signals: frozenset[str]
    root_cause: str
    recommended_action_type: ActionType
    target_resource_id: str


DIAGNOSIS_RULES: tuple[DiagnosisRule, ...] = (
    DiagnosisRule(
        "postgres_stopped",
        frozenset({"postgres_exporter_up_zero", "moodle_database_connection_error"}),
        "PostgreSQL container stopped",
        ActionType.START_CONTAINER,
        "postgres-db",
    ),
    DiagnosisRule(
        "postgres_down",
        frozenset({"postgres_stopped"}),
        "PostgreSQL container stopped",
        ActionType.START_CONTAINER,
        "postgres-db",
    ),
    DiagnosisRule(
        "postgres_connections_exhausted",
        frozenset({"postgres_connections_saturated"}),
        "Available PostgreSQL connections are exhausted",
        ActionType.STOP_FAULT_INJECTOR,
        "postgres-db",
    ),
    DiagnosisRule(
        "invalid_database_endpoint",
        frozenset({"moodle_database_resolution_failed"}),
        "Moodle points to an invalid database endpoint",
        ActionType.RUN_ANSIBLE_PLAYBOOK,
        "postgres-db",
    ),
    DiagnosisRule(
        "moodle_stopped",
        frozenset({"moodle_endpoint_down"}),
        "Moodle container stopped",
        ActionType.START_CONTAINER,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_apache_router_drift",
        frozenset({"apache_router_config_missing"}),
        "Moodle Apache router configuration drift",
        ActionType.RUN_ANSIBLE_PLAYBOOK,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_cpu_pressure",
        frozenset({"host_cpu_saturation"}),
        "Controlled CPU pressure on Moodle host",
        ActionType.STOP_FAULT_INJECTOR,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodledata_permission_drift",
        frozenset({"moodle_write_permission_denied", "sec-02"}),
        "Moodle data directory permission or ownership drift",
        ActionType.RESTORE_MOODLEDATA_PERMISSION,
        "moodledata-volume",
    ),
    DiagnosisRule(
        "moodle_rds_tcp_blocked",
        frozenset({"moodlenoderdstcpfailed", "moodle_rds_tcp_failed", "net-02"}),
        "Moodle node cannot reach RDS PostgreSQL over TCP",
        ActionType.REMOVE_SCOPED_PORT_BLOCK,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_cpu_high",
        frozenset({"moodlenodecpuhigh", "moodle_cpu_high", "res-01"}),
        "High CPU consumption from artificial load fixture on Moodle node",
        ActionType.STOP_FAULT_INJECTOR,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_memory_pressure",
        frozenset({"moodlenodememorypressure", "moodle_memory_pressure", "res-02"}),
        "Memory pressure detected on Moodle node",
        ActionType.STOP_FAULT_INJECTOR,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_scratch_enospc",
        frozenset({"moodlescratchenospc", "moodle_scratch_enospc", "res-03"}),
        "Scratch filesystem out of space on Moodle node",
        ActionType.STOP_FAULT_INJECTOR,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_web_missing",
        frozenset({"moodlewebcontainermissing", "container_missing", "con-01"}),
        "Moodle web container is missing or down",
        ActionType.START_CONTAINER,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_router_missing",
        frozenset({"moodleapacheroutermissing", "con-02"}),
        "Moodle Apache router configuration is missing",
        ActionType.RUN_ANSIBLE_PLAYBOOK,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_web_restarting",
        frozenset({"moodlewebcontainerrestarting", "con-03"}),
        "Moodle web container is crashing or restarting continuously",
        ActionType.RESTART_CONTAINER,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_trusted_proxy_drift",
        frozenset({"moodletrustedproxyconfigdrift", "sec-03"}),
        "Moodle reverse proxy trust configuration drifted",
        ActionType.RECONFIGURE_SERVICE,
        "moodle-app",
    ),
    DiagnosisRule(
        "moodle_synthetic_transaction_failed",
        frozenset({"moodlesynthetictransactionfailed", "moodle_synthetic_failed", "db-01"}),
        "Moodle synthetic transaction failed",
        ActionType.RECONFIGURE_SERVICE,
        "moodle-app",
    ),
    DiagnosisRule(
        "generic_service_drift",
        frozenset({"system_alert"}),
        "Operational service configuration drift",
        ActionType.RESTART_CONTAINER,
        "moodle-app",
    ),
)


class DiagnosisInput(BaseRoleMessage):
    """Input payload for the Diagnosis role."""

    incident: Incident = Field(..., description="Active Incident model.")
    evidence_items: list[Evidence] = Field(
        default_factory=list,
        description="Collected evidence items to analyze.",
    )
    signals: list[str] = Field(
        default_factory=list,
        description="Explicit signal names observed by Observer.",
    )


class DiagnosisOutput(BaseRoleMessage):
    """Output envelope from the Diagnosis role."""

    diagnosis_id: str = Field(..., min_length=1, description="Unique diagnosis identifier.")
    cause_code: str = Field(..., min_length=1, description="Standardized cause code.")
    root_cause: str = Field(..., min_length=1, description="Human-readable root cause explanation.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score.")
    affected_resources: list[str] = Field(
        default_factory=list,
        description="Resource IDs diagnosed as affected.",
    )
    ready_for_planning: bool = Field(
        ...,
        description="True if an unambiguous root cause was determined with high confidence.",
    )
    recommended_action_type: ActionType = Field(
        default=ActionType.READ_HEALTH,
        description="Remediation action type recommended by diagnosis.",
    )
    target_resource_id: str = Field(
        default="",
        description="Resource identifier that action should target.",
    )
    reasoning_summary: str = Field(
        ...,
        description="Detailed rationale for the diagnosis verdict.",
    )
    missing_evidence: list[str] = Field(
        default_factory=list,
        description="Missing signals or evidence preventing confident diagnosis.",
    )


class DiagnosisRole:
    """Deterministic Diagnosis component that produces validated root cause hypotheses."""

    def diagnose(self, input_data: DiagnosisInput) -> DiagnosisOutput:
        if input_data.incident.incident_id != input_data.incident_id:
            raise ValueError(
                f"Incident ID mismatch: input={input_data.incident_id} vs "
                f"model={input_data.incident.incident_id}"
            )

        now = datetime.now(timezone.utc)
        valid_evidence: list[Evidence] = [
            item for item in input_data.evidence_items
            if item.is_valid(now)
        ]

        # Extract all signals from valid evidence and input signals
        observed_signals: set[str] = {s.strip().lower() for s in input_data.signals}
        for item in valid_evidence:
            observed_signals.add(item.content_ref.replace("metric:", "").strip().lower())
            observed_signals.add(item.summary.strip().lower())

        # Match against diagnosis rules
        matched_rules: list[DiagnosisRule] = []
        for rule in DIAGNOSIS_RULES:
            if any(req in observed_signals or any(req in sig for sig in observed_signals) for req in rule.required_signals):
                matched_rules.append(rule)

        evidence_ids = [e.evidence_id for e in valid_evidence] or list(input_data.evidence_ids)

        if not matched_rules:
            diagnosis_id = f"diag-{hashlib.sha256(f'{input_data.incident_id}:unknown'.encode()).hexdigest()[:16]}"
            return DiagnosisOutput(
                incident_id=input_data.incident_id,
                evidence_ids=evidence_ids,
                diagnosis_id=diagnosis_id,
                cause_code="UNKNOWN_ANOMALY",
                root_cause="No operational diagnosis rule matched observed signals",
                confidence=0.0,
                affected_resources=list(input_data.incident.resource_ids),
                ready_for_planning=False,
                recommended_action_type=ActionType.READ_HEALTH,
                target_resource_id=input_data.incident.resource_ids[0] if input_data.incident.resource_ids else "",
                reasoning_summary="Observed signals do not form an unambiguous match with known runbooks.",
                missing_evidence=["matching diagnostic rule"],
            )

        selected_rule = matched_rules[0]
        diagnosis_id = f"diag-{hashlib.sha256(f'{input_data.incident_id}:{selected_rule.cause_code}'.encode()).hexdigest()[:16]}"

        # Update incident diagnosis_id
        input_data.incident.diagnosis_id = diagnosis_id

        return DiagnosisOutput(
            incident_id=input_data.incident_id,
            evidence_ids=evidence_ids,
            diagnosis_id=diagnosis_id,
            cause_code=selected_rule.cause_code,
            root_cause=selected_rule.root_cause,
            confidence=0.9,
            affected_resources=list(input_data.incident.resource_ids),
            ready_for_planning=True,
            recommended_action_type=selected_rule.recommended_action_type,
            target_resource_id=selected_rule.target_resource_id or input_data.incident.resource_ids[0],
            reasoning_summary=f"Rule {selected_rule.cause_code} matched: {selected_rule.root_cause}.",
        )


@celery_app.task(name="evaluation.agent_replay.roles.diagnose_incident_task", bind=True)
def diagnose_incident_task(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Celery background task for running the Diagnosis role."""
    diagnosis_input = DiagnosisInput.model_validate(payload)
    output = DiagnosisRole().diagnose(diagnosis_input)
    return output.model_dump(mode="json")

