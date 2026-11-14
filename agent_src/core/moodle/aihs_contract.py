"""A3 AIHS proposal contract. No alert dispatch, approval, or execution path.

The public AIHS prototype returns only a server action code. This boundary
keeps model output untrusted and requires a separately reviewed Moodle mapping
and fresh incident evidence before emitting even a shadow candidate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import DatabaseError
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator

from core.environment_profile import get_environment_profile
from core.evidence.evidence_store import EvidenceIntegrityError, SQLiteEvidenceStore
from core.safety.action_catalog import ActionCatalog
from core.schema.common import ResourceType


AIHS_ACTIONS = frozenset({"NOP", "MSR", "ER", "RB", "RI", "RMA"})
EVIDENCE_SOURCES = frozenset({"alertmanager", "log_watcher", "prometheus", "resource_inventory"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AIHSOutput(_Strict):
    hostname: StrictStr = Field(min_length=1, max_length=253)
    timestamp: StrictInt = Field(ge=0)
    action: list[StrictStr] = Field(min_length=1, max_length=1)


class DecisionContext(_Strict):
    incident_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(pattern=r"^[A-Z]{2,3}-[0-9]{2}$")
    environment: Literal["staging", "production"]
    trusted_target_scope: str = Field(min_length=1, max_length=100)
    trusted_target_resource_id: str = Field(min_length=1, max_length=100)
    trusted_target_resource_type: ResourceType
    evidence_refs: list[str] = Field(max_length=48)
    predicted_at: datetime
    model_version: str = Field(min_length=1, max_length=128)
    data_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_timezone(self) -> DecisionContext:
        if self.predicted_at.tzinfo is None or self.predicted_at.utcoffset() is None:
            raise ValueError("predicted_at must have a timezone")
        return self


class MappingEntry(_Strict):
    aihs_action: Literal["NOP", "MSR", "ER", "RB", "RI", "RMA"]
    status: Literal["unmapped", "human_only", "shadow_reviewed", "live_reviewed"]
    catalog_action_id: str | None = None
    scenario_id: str | None = None
    target_scope: str | None = None
    target_resource_id: str | None = None
    target_resource_type: ResourceType | None = None
    required_role: Literal["executor", "verifier"] | None = None
    review_reference: str | None = None
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_mapping(self) -> MappingEntry:
        bound = (self.catalog_action_id, self.scenario_id, self.target_scope,
                 self.target_resource_id, self.target_resource_type, self.required_role)
        if self.status in {"shadow_reviewed", "live_reviewed"} and not all(bound):
            raise ValueError("reviewed mapping requires action, scenario, target, type and role")
        if self.status == "live_reviewed" and not self.review_reference:
            raise ValueError("live mapping requires an independent review reference")
        if self.status != "live_reviewed" and self.review_reference is not None:
            raise ValueError("review reference is reserved for live-reviewed mapping")
        if self.status not in {"shadow_reviewed", "live_reviewed"} and any(item is not None for item in bound):
            raise ValueError("unmapped or human-only action cannot contain a catalog mapping")
        return self


class MappingDefinition(_Strict):
    schema_version: Literal[1]
    mapping_version: str = Field(min_length=1, max_length=128)
    environment: Literal["staging", "production"]
    review_status: Literal["no_approved_action_mapping", "reviewed_for_shadow", "reviewed_for_live"]
    actions: list[MappingEntry] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def complete_action_set(self) -> MappingDefinition:
        if {entry.aihs_action for entry in self.actions} != AIHS_ACTIONS:
            raise ValueError("mapping must cover each AIHS action exactly once")
        if self.review_status == "reviewed_for_live" and not any(
            entry.status == "live_reviewed" for entry in self.actions
        ):
            raise ValueError("live mapping must contain a live-reviewed action")
        return self


class AIHSAdapterResult(_Strict):
    status: Literal["HUMAN_REVIEW", "SHADOW_CANDIDATE"]
    reason_code: Literal[
        "model_unavailable", "invalid_model_output", "unknown_aihs_action",
        "human_only_action", "missing_evidence", "stale_evidence",
        "cross_incident_evidence", "invalid_evidence", "stale_prediction",
        "unmapped_action", "scope_mismatch", "catalog_not_allowed", "shadow_candidate_ready",
    ]
    incident_id: str
    scenario_id: str
    model_version: str
    data_version: str
    mapping_version: str
    predicted_at: datetime
    aihs_action: str | None = None
    catalog_action_id: str | None = None
    target_scope: str | None = None
    target_resource_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    execution_permitted: Literal[False] = False
    dispatched: Literal[False] = False


def load_mapping(path: str | Path) -> MappingDefinition:
    return MappingDefinition.model_validate_json(Path(path).read_text())


def adapt_aihs_output(
    raw_output: Any,
    *,
    context: DecisionContext,
    evidence_db: str | Path,
    mapping: MappingDefinition,
    now: datetime | None = None,
    catalog: ActionCatalog | None = None,
) -> AIHSAdapterResult:
    """Return a non-executable, typed proposal or a reason to hand off."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must have a timezone")

    def result(reason_code: str, *, mapped: MappingEntry | None = None,
               action_code: str | None = None) -> AIHSAdapterResult:
        return AIHSAdapterResult(
            status="SHADOW_CANDIDATE" if reason_code == "shadow_candidate_ready" else "HUMAN_REVIEW",
            reason_code=reason_code,
            incident_id=context.incident_id,
            scenario_id=context.scenario_id,
            model_version=context.model_version,
            data_version=context.data_version,
            mapping_version=mapping.mapping_version,
            predicted_at=context.predicted_at,
            aihs_action=action_code,
            catalog_action_id=mapped.catalog_action_id if mapped else None,
            target_scope=mapped.target_scope if mapped else None,
            target_resource_id=mapped.target_resource_id if mapped else None,
            evidence_refs=list(context.evidence_refs),
        )

    if raw_output is None:
        return result("model_unavailable")
    try:
        output = AIHSOutput.model_validate(raw_output)
    except (ValidationError, TypeError, ValueError):
        return result("invalid_model_output")
    action_code = output.action[0]
    if action_code not in AIHS_ACTIONS:
        return result("unknown_aihs_action")
    if action_code == "RMA":
        return result("human_only_action", action_code=action_code)

    profile = get_environment_profile()
    if context.environment != profile.environment or mapping.environment != context.environment:
        return result("scope_mismatch", action_code=action_code)
    if (context.predicted_at > current + timedelta(seconds=30)
            or current - context.predicted_at > timedelta(seconds=profile.evidence.max_sample_age_seconds)):
        return result("stale_prediction", action_code=action_code)
    if (len(context.evidence_refs) < profile.gate.min_evidence_refs
            or len(context.evidence_refs) != len(set(context.evidence_refs))):
        return result("missing_evidence", action_code=action_code)
    if not Path(evidence_db).is_file():
        return result("missing_evidence", action_code=action_code)
    try:
        with SQLiteEvidenceStore(evidence_db) as store:
            store.verify_integrity()
            evidence = [store.get_by_id(ref) for ref in context.evidence_refs]
    except (EvidenceIntegrityError, ValueError, DatabaseError, OSError):
        return result("invalid_evidence", action_code=action_code)
    if any(item is None for item in evidence):
        return result("missing_evidence", action_code=action_code)
    if any(item.incident_id != context.incident_id for item in evidence):
        return result("cross_incident_evidence", action_code=action_code)
    if any(item.source not in EVIDENCE_SOURCES for item in evidence):
        return result("invalid_evidence", action_code=action_code)
    freshness = timedelta(seconds=profile.evidence.max_sample_age_seconds)
    if any(
        item.collected_at > context.predicted_at
        or item.observed_at > context.predicted_at
        or context.predicted_at - item.collected_at > freshness
        or context.predicted_at - item.observed_at > freshness
        for item in evidence
    ):
        return result("stale_evidence", action_code=action_code)

    rule = next(entry for entry in mapping.actions if entry.aihs_action == action_code)
    if (mapping.review_status == "no_approved_action_mapping"
            or rule.status not in {"shadow_reviewed", "live_reviewed"}):
        return result("unmapped_action", action_code=action_code)
    if (rule.scenario_id != context.scenario_id
            or rule.target_scope != context.trusted_target_scope
            or rule.target_resource_id != context.trusted_target_resource_id
            or rule.target_resource_type != context.trusted_target_resource_type):
        return result("scope_mismatch", action_code=action_code)
    catalog = catalog or ActionCatalog.load_moodle_catalog()
    if (catalog.lookup(rule.catalog_action_id) is None
            or not catalog.target_type_allowed(rule.catalog_action_id, rule.target_resource_type)
            or not catalog.is_safe_execution_allowed(
                rule.catalog_action_id, scenario_id=rule.scenario_id,
                target_scope=rule.target_scope, environment=context.environment,
                role=rule.required_role,
            )):
        return result("catalog_not_allowed", action_code=action_code)
    return result("shadow_candidate_ready", mapped=rule, action_code=action_code)
