"""Guarded incident and action lifecycle transitions."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import ClassVar

from pydantic import ValidationError

from core.environment_profile import get_environment_profile
from core.schema.action import TypedAction
from core.schema.audit import AuditEvent
from core.schema.common import ActionDecision, ActionStatus, IncidentStatus
from core.schema.incident import Incident
from core.schema.verification import VerificationResult


class StateTransitionError(ValueError):
    def __init__(
        self,
        current_state: IncidentStatus,
        requested_state: IncidentStatus,
        reason: str,
    ) -> None:
        self.current_state = current_state
        self.requested_state = requested_state
        self.reason = reason
        super().__init__(
            f"invalid incident transition {current_state.value} -> "
            f"{requested_state.value}: {reason}"
        )


class IncidentStateMachine:
    ALLOWED_TRANSITIONS: ClassVar[dict[IncidentStatus, frozenset[IncidentStatus]]] = {
        IncidentStatus.OPEN: frozenset({IncidentStatus.TRIAGED, IncidentStatus.FAILED}),
        IncidentStatus.TRIAGED: frozenset(
            {IncidentStatus.PLANNED, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
        ),
        IncidentStatus.PLANNED: frozenset(
            {IncidentStatus.GATED, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
        ),
        IncidentStatus.GATED: frozenset(
            {IncidentStatus.EXECUTED, IncidentStatus.ESCALATED, IncidentStatus.FAILED}
        ),
        IncidentStatus.ESCALATED: frozenset(
            {IncidentStatus.PLANNED, IncidentStatus.FAILED}
        ),
        IncidentStatus.EXECUTED: frozenset(
            {IncidentStatus.VERIFYING, IncidentStatus.FAILED}
        ),
        IncidentStatus.VERIFYING: frozenset(
            {IncidentStatus.RESOLVED, IncidentStatus.VERIFIED_DRY_RUN, IncidentStatus.FAILED}
        ),
        IncidentStatus.VERIFIED_DRY_RUN: frozenset(),
        IncidentStatus.RESOLVED: frozenset(),
        IncidentStatus.FAILED: frozenset(),
    }

    def transition(
        self,
        incident: Incident,
        requested_state: IncidentStatus,
        *,
        actor: str,
        reason: str,
        evidence_refs: list[str] | None = None,
    ) -> AuditEvent:
        if requested_state in {IncidentStatus.RESOLVED, IncidentStatus.VERIFIED_DRY_RUN}:
            raise StateTransitionError(
                incident.status,
                requested_state,
                "verifier terminal states require resolve_verified with an eligible VerificationResult",
            )
        return self._apply_transition(
            incident,
            requested_state,
            actor=actor,
            reason=reason,
            evidence_refs=evidence_refs,
        )

    def resolve_verified(
        self,
        incident: Incident,
        verification: VerificationResult,
        *,
        reason: str,
        evidence_refs: list[str] | None = None,
    ) -> AuditEvent:
        """Resolve only from a complete, eligible verifier result for this incident."""
        try:
            verification = VerificationResult.model_validate(
                verification.model_dump(mode="python", warnings=False)
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise StateTransitionError(
                incident.status,
                IncidentStatus.RESOLVED,
                "independent verifier result failed schema validation",
            ) from exc
        if (
            verification.incident_id != incident.incident_id
            or verification.verdict != "resolved"
            or not verification.resolution_eligible
            or not verification.health_passed
            or not verification.communication_contract_passed
            or (not verification.simulated and any(
                item.simulated
                for item in (
                    *verification.stability_observations,
                    *verification.allowed_probes,
                    *verification.forbidden_probes,
                    *verification.related_probes,
                )
            ))
            or not _has_stable_window(verification)
            or not _has_complete_probe_sets(verification)
        ):
            raise StateTransitionError(
                incident.status,
                IncidentStatus.RESOLVED,
                "independent verifier result is missing, ineligible, or failed",
            )
        terminal_state = (
            IncidentStatus.VERIFIED_DRY_RUN
            if verification.simulated
            else IncidentStatus.RESOLVED
        )
        return self._apply_transition(
            incident,
            terminal_state,
            actor="independent_verifier",
            reason=(
                f"Simulated verification only: {reason}"
                if verification.simulated
                else reason
            ),
            evidence_refs=evidence_refs,
            verifier_attestation_ref=verification.verification_id,
        )

    def _apply_transition(
        self,
        incident: Incident,
        requested_state: IncidentStatus,
        *,
        actor: str,
        reason: str,
        evidence_refs: list[str] | None = None,
        verifier_attestation_ref: str | None = None,
    ) -> AuditEvent:
        current_state = incident.status
        if requested_state not in self.ALLOWED_TRANSITIONS[current_state]:
            raise StateTransitionError(current_state, requested_state, reason)
        if requested_state == IncidentStatus.RESOLVED and not (
            verifier_attestation_ref and verifier_attestation_ref.strip()
        ):
            raise StateTransitionError(
                current_state, requested_state, "RESOLVED requires a verifier attestation reference"
            )

        occurred_at = datetime.now(timezone.utc)
        refs = list(dict.fromkeys(evidence_refs or []))
        event_seed = (
            f"{incident.incident_id}|{current_state.value}|{requested_state.value}|"
            f"{actor}|{reason}|{occurred_at.isoformat()}"
        )
        event_id = (
            "audit-" + hashlib.sha256(event_seed.encode("utf-8")).hexdigest()[:16]
        )
        event = AuditEvent(
            event_id=event_id,
            incident_id=incident.incident_id,
            old_state=current_state,
            new_state=requested_state,
            actor=actor,
            reason=reason,
            evidence_refs=refs,
            occurred_at=occurred_at,
        )
        # Incident.__setattr__ blocks direct mutation of status and the verifier
        # markers; only this validated, audited state-machine path may apply them.
        if requested_state == IncidentStatus.RESOLVED:
            object.__setattr__(incident, "verifier_attestation_ref", verifier_attestation_ref)
            object.__setattr__(incident, "resolved_by_verifier", True)
        object.__setattr__(incident, "status", requested_state)
        incident.updated_at = occurred_at
        return event


def _has_stable_window(verification: VerificationResult) -> bool:
    policy = get_environment_profile().verification
    observations = verification.stability_observations
    if len(observations) < policy.min_samples or not all(item.healthy for item in observations):
        return False
    timestamps = [item.observed_at for item in observations]
    if timestamps != sorted(timestamps):
        return False
    measured_window = int((timestamps[-1] - timestamps[0]).total_seconds())
    if measured_window < policy.stability_window_seconds or verification.stability_seconds != measured_window:
        return False
    # Same freshness bounds as the attestation check: one policy, no drift.
    latest_age = (datetime.now(timezone.utc) - timestamps[-1].astimezone(timezone.utc)).total_seconds()
    if latest_age > policy.max_attestation_age_seconds or latest_age < -policy.max_future_skew_seconds:
        return False
    max_gap = 65 if len(observations) == 3 else 60
    return all(
        0 < (later - earlier).total_seconds() <= max_gap
        for earlier, later in zip(timestamps, timestamps[1:])
    )


def _has_complete_probe_sets(verification: VerificationResult) -> bool:
    groups = (
        verification.allowed_probes,
        verification.forbidden_probes,
        verification.related_probes,
    )
    return all(
        probes
        and all(probe.passed and probe.name.strip() for probe in probes)
        and len({probe.name for probe in probes}) == len(probes)
        for probes in groups
    )


def apply_gate_decision(action: TypedAction, decision: ActionDecision) -> ActionStatus:
    if decision == ActionDecision.ALLOW:
        action.status = ActionStatus.GATED
    elif decision == ActionDecision.REQUIRE_APPROVAL:
        action.status = ActionStatus.AWAITING_APPROVAL
    elif decision == ActionDecision.HUMAN_ONLY:
        action.status = ActionStatus.DENIED
        action.requires_approval = True
    else:
        action.status = ActionStatus.DENIED
    return action.status
