"""C0 (aihs-paper-repro-moodle-v1) proposal contract. No dispatch, approval or execution path.

The locked C0 model proposes a Moodle catalog action id (or the observe-only
``wait_and_recheck`` / ``call_human`` rungs), not an Alibaba server code. The
proposal is untrusted model output: it can only select an entry of a separately
reviewed mapping, and the incident scenario/target/evidence come from trusted
context, never from the proposal, ground-truth fixtures or labels.

A mapping entry that has not been reviewed by a human domain owner is
``draft_unreviewed``. It never produces a candidate unless the caller passes
``allow_draft_for_replay=True`` (offline shadow replay only); the staging handoff
still requires ``live_reviewed`` plus a ``review_reference``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictStr, ValidationError, model_validator

from core.moodle.aihs_contract import AIHSAdapterResult, DecisionContext, check_decision_evidence
from core.safety.action_catalog import ActionCatalog
from core.schema.common import ResourceType

C0_BASELINE_ID = "aihs-paper-repro-moodle-v1"
# Mirrors evaluation/aihs/action_ladder.PREDICTOR_CLASSES (8 classes); kept explicit so
# agent_src has no dependency on the evaluation tree.
C0_MUTATING_ACTIONS = frozenset({
    "remove_scoped_db_reject", "recreate_moodle_web_from_reviewed_compose",
    "remove_named_cpu_load_container", "start_reviewed_compose_service",
    "restore_fixture_directory_mode", "restart_moodle_cron_service",
})
C0_ACTIONS = C0_MUTATING_ACTIONS | {"wait_and_recheck", "call_human"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControllerStep(_Strict):
    action: StrictStr
    controller: StrictStr
    p_success: StrictFloat | None = None
    predicted_success: bool | None = None


class C0Proposal(_Strict):
    """One locked-C0 proposal row (no label, outcome or scenario field exists)."""

    baseline_id: Literal["aihs-paper-repro-moodle-v1"]
    controller_trace: list[ControllerStep]
    evidence_cutoff_at: datetime
    model_version: StrictStr = Field(min_length=1, max_length=128)
    path: StrictStr
    predicted_at: datetime
    predictor_action: StrictStr
    proposed_action: StrictStr = Field(min_length=1, max_length=64)
    record_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    record_id: StrictStr = Field(pattern=r"^a2-[0-9]{4}$")
    snapshot_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    split: Literal["train", "validation", "test"]
    status: Literal["LOCKED_C0"]


class C0MappingEntry(_Strict):
    aihs_action: StrictStr  # C0 action id; field name kept so shared Gate/handoff code can duck-type it
    status: Literal["draft_unreviewed", "observe_only", "human_only", "shadow_reviewed", "live_reviewed"]
    catalog_action_id: str | None = None
    scenario_id: str | None = None
    target_scope: str | None = None
    target_resource_id: str | None = None
    target_resource_type: ResourceType | None = None
    required_role: Literal["executor", "verifier"] | None = None
    rollback_action_id: str | None = None
    verifier_probe_id: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    review_reference: str | None = None
    reason: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def validate_entry(self) -> C0MappingEntry:
        bound = (self.catalog_action_id, self.scenario_id, self.target_scope, self.target_resource_id,
                 self.target_resource_type, self.required_role, self.rollback_action_id, self.verifier_probe_id)
        if self.aihs_action not in C0_ACTIONS:
            raise ValueError("action is outside the C0 action space")
        if self.status in {"draft_unreviewed", "shadow_reviewed", "live_reviewed"}:
            if not all(bound) or self.catalog_action_id != self.aihs_action:
                raise ValueError("bound mapping requires action, scenario, target, type, role, rollback and probe")
        elif any(item is not None for item in bound):
            raise ValueError("observe-only or human-only action cannot contain a catalog mapping")
        if self.status == "live_reviewed" and not self.review_reference:
            raise ValueError("live mapping requires an independent review reference")
        if self.status != "live_reviewed" and self.review_reference is not None:
            raise ValueError("review reference is reserved for live-reviewed mapping")
        return self


class C0MappingDefinition(_Strict):
    schema_version: Literal[1]
    mapping_version: str = Field(min_length=1, max_length=128)
    environment: Literal["staging", "production"]
    review_status: Literal["draft_pending_domain_review", "reviewed_for_shadow", "reviewed_for_live"]
    actions: list[C0MappingEntry] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def complete_action_set(self) -> C0MappingDefinition:
        if {entry.aihs_action for entry in self.actions} != C0_ACTIONS:
            raise ValueError("mapping must cover each C0 action exactly once")
        live = any(entry.status == "live_reviewed" for entry in self.actions)
        if self.review_status == "reviewed_for_live" and not live:
            raise ValueError("live mapping must contain a live-reviewed action")
        if self.review_status == "draft_pending_domain_review" and any(
            entry.status in {"shadow_reviewed", "live_reviewed"} for entry in self.actions
        ):
            raise ValueError("a draft mapping cannot contain reviewed entries")
        return self


def load_c0_mapping(path: str | Path) -> C0MappingDefinition:
    return C0MappingDefinition.model_validate_json(Path(path).read_text(encoding="utf-8"))


def adapt_c0_proposal(
    raw_proposal: Any,
    *,
    context: DecisionContext,
    evidence_db: str | Path,
    mapping: C0MappingDefinition,
    now: datetime | None = None,
    catalog: ActionCatalog | None = None,
    allow_draft_for_replay: bool = False,
) -> AIHSAdapterResult:
    """Return a non-executable typed candidate, or a reason to hand off to a human."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must have a timezone")

    def result(reason_code: str, *, mapped: C0MappingEntry | None = None,
               action_code: str | None = None) -> AIHSAdapterResult:
        candidate = reason_code in {"shadow_candidate_ready", "draft_shadow_candidate"}
        return AIHSAdapterResult(
            status="SHADOW_CANDIDATE" if candidate else "HUMAN_REVIEW",
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

    if raw_proposal is None:
        return result("model_unavailable")
    try:
        proposal = raw_proposal if isinstance(raw_proposal, C0Proposal) else C0Proposal.model_validate(raw_proposal)
    except (ValidationError, TypeError, ValueError):
        return result("invalid_model_output")
    action_code = proposal.proposed_action
    if action_code not in C0_ACTIONS:
        return result("unknown_aihs_action")
    rule = next(entry for entry in mapping.actions if entry.aihs_action == action_code)
    if rule.status == "human_only":
        return result("human_only_action", action_code=action_code)
    if rule.status == "observe_only":
        return result("observe_only_action", action_code=action_code)

    failure = check_decision_evidence(
        context, mapping_environment=mapping.environment, evidence_db=evidence_db, current=current,
    )
    if failure is not None:
        return result(failure, action_code=action_code)

    draft_ok = rule.status == "draft_unreviewed" and allow_draft_for_replay
    if (rule.status == "draft_unreviewed" and not draft_ok) or (
        mapping.review_status == "draft_pending_domain_review" and not allow_draft_for_replay
    ):
        return result("unmapped_action", action_code=action_code)
    if (rule.scenario_id != context.scenario_id
            or rule.target_scope != context.trusted_target_scope
            or rule.target_resource_id != context.trusted_target_resource_id
            or rule.target_resource_type != context.trusted_target_resource_type):
        return result("scope_mismatch", action_code=action_code)
    catalog = catalog or ActionCatalog.load_moodle_7_scenario_catalog()
    if (catalog.lookup(rule.catalog_action_id) is None
            or not catalog.target_type_allowed(rule.catalog_action_id, rule.target_resource_type)
            or not catalog.is_safe_execution_allowed(
                rule.catalog_action_id, scenario_id=rule.scenario_id,
                target_scope=rule.target_scope, environment=context.environment,
                role=rule.required_role,
            )):
        return result("catalog_not_allowed", action_code=action_code)
    return result("draft_shadow_candidate" if draft_ok else "shadow_candidate_ready",
                  mapped=rule, action_code=action_code)
