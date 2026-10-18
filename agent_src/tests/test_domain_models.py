"""Tests for evaluation.agent_replay.domain — Phase 1 domain models.

Coverage
--------
- ActionType rejects unknown values (closed enum).
- Evidence.is_valid() returns True/False based on expires_at.
- Evidence rejects naive datetimes and inverted timestamps.
- ActionPlan.plan_hash is stable regardless of dict key order.
- ActionPlan.plan_hash changes when any field changes.
- ActionPlan rejects BLOCKED_UNRESTRICTED_SHELL.
- ActionPlan rejects a caller-supplied plan_hash.
- ActionPlan rejects target_resource_id not in expected_scope.
- ReversibleRollback requires at least one step.
- IrreversibleRollback requires a non-blank reason.
- Resource rejects non-string label values.
- Incident model round-trips cleanly.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from evaluation.agent_replay.domain.enums import ActionType, Environment, EvidenceSource
from evaluation.agent_replay.domain.evidence import Evidence
from evaluation.agent_replay.domain.incident import Incident, IncidentStatus
from evaluation.agent_replay.domain.resource import Resource
from evaluation.agent_replay.domain.action_plan import (
    ActionPlan,
    BlastRadius,
    ExpectedScope,
    IrreversibleRollback,
    ReversibleRollback,
    _compute_plan_hash,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_LATER = _NOW + timedelta(minutes=10)
_PAST = _NOW - timedelta(seconds=1)


def _make_evidence(**overrides) -> Evidence:
    defaults = dict(
        evidence_id="ev-001",
        source=EvidenceSource.METRIC,
        collected_at=_NOW,
        expires_at=_LATER,
        resource_ids=["moodle-app"],
        content_ref="sha256:abc123",
        summary="PostgreSQL exporter reports up=0",
    )
    defaults.update(overrides)
    return Evidence(**defaults)


def _make_scope(**overrides) -> ExpectedScope:
    defaults = dict(
        affected_resource_ids=["moodle-app"],
        max_blast_radius=BlastRadius.LOW,
    )
    defaults.update(overrides)
    return ExpectedScope(**defaults)


def _make_reversible_rollback(**overrides) -> ReversibleRollback:
    defaults = dict(
        steps=["stop moodle-app compose service"],
        max_rollback_seconds=60,
    )
    defaults.update(overrides)
    return ReversibleRollback(**defaults)


def _make_plan(**overrides) -> ActionPlan:
    defaults = dict(
        incident_id="inc-001",
        action_type=ActionType.START_CONTAINER,
        target_resource_id="moodle-app",
        operation="start moodle-app via Docker Compose",
        evidence_ids=["ev-001", "ev-002", "ev-003"],
        expected_scope=_make_scope(),
        expected_outcome="Moodle synthetic transaction passes within 60 s.",
        rollback=_make_reversible_rollback(),
        requested_by="planner_agent",
        created_at=_NOW,
    )
    defaults.update(overrides)
    return ActionPlan(**defaults)


# ===========================================================================
# ActionType — closed enum
# ===========================================================================


class TestActionTypeClosedEnum:
    def test_valid_values_accepted(self):
        assert ActionType("start_container") is ActionType.START_CONTAINER
        assert ActionType("read_health") is ActionType.READ_HEALTH

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError):
            ActionType("run_arbitrary_shell_command")

    def test_unknown_value_rejected_2(self):
        with pytest.raises(ValueError):
            ActionType("rm -rf /")

    def test_blocked_shell_sentinel_is_a_member(self):
        """BLOCKED_UNRESTRICTED_SHELL must be part of the enum (as a sentinel)."""
        assert ActionType.BLOCKED_UNRESTRICTED_SHELL in ActionType

    def test_all_members_are_strings(self):
        for member in ActionType:
            assert isinstance(member.value, str)


# ===========================================================================
# Evidence — expiry and validation
# ===========================================================================


class TestEvidenceIsValid:
    def test_valid_before_expiry(self):
        ev = _make_evidence()
        assert ev.is_valid(_NOW) is True

    def test_invalid_at_expiry(self):
        ev = _make_evidence()
        assert ev.is_valid(_LATER) is False  # expires_at is exclusive upper bound

    def test_invalid_after_expiry(self):
        ev = _make_evidence()
        assert ev.is_valid(_LATER + timedelta(seconds=1)) is False

    def test_uses_utcnow_when_no_arg(self):
        # Evidence that expired long ago should always be invalid.
        ev = _make_evidence(
            collected_at=_NOW - timedelta(hours=2),
            expires_at=_NOW - timedelta(hours=1),
        )
        assert ev.is_valid() is False

    def test_valid_evidence_just_before_expiry(self):
        ev = _make_evidence()
        just_before = _LATER - timedelta(milliseconds=1)
        assert ev.is_valid(just_before) is True


class TestEvidenceValidation:
    def test_rejects_naive_collected_at(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _make_evidence(collected_at=datetime(2026, 1, 1, 12, 0, 0))

    def test_rejects_naive_expires_at(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _make_evidence(expires_at=datetime(2026, 1, 1, 13, 0, 0))

    def test_rejects_expires_before_collected(self):
        with pytest.raises(ValueError, match="strictly after collected_at"):
            _make_evidence(expires_at=_NOW - timedelta(seconds=1))

    def test_rejects_expires_equal_collected(self):
        with pytest.raises(ValueError, match="strictly after collected_at"):
            _make_evidence(expires_at=_NOW)

    def test_rejects_empty_resource_ids(self):
        with pytest.raises(ValueError):
            _make_evidence(resource_ids=[])

    def test_rejects_blank_resource_id(self):
        with pytest.raises(ValueError):
            _make_evidence(resource_ids=["  "])

    def test_rejects_naive_now_argument(self):
        ev = _make_evidence()
        with pytest.raises(ValueError, match="timezone-aware"):
            ev.is_valid(datetime(2026, 1, 1, 12, 0, 0))  # naive

    def test_multiple_resource_ids_accepted(self):
        ev = _make_evidence(resource_ids=["moodle-app", "postgres-db"])
        assert ev.resource_ids == ["moodle-app", "postgres-db"]

    def test_immutable_after_construction(self):
        ev = _make_evidence()
        with pytest.raises(Exception):  # frozen model raises ValidationError or TypeError
            ev.summary = "tampered"  # type: ignore[misc]


# ===========================================================================
# ActionPlan.plan_hash — stability and sensitivity
# ===========================================================================


class TestPlanHash:
    def test_hash_is_hex_string(self):
        plan = _make_plan()
        assert isinstance(plan.plan_hash, str)
        assert len(plan.plan_hash) == 64
        int(plan.plan_hash, 16)  # must be valid hex

    def test_hash_stable_across_identical_plans(self):
        plan_a = _make_plan()
        plan_b = _make_plan()
        assert plan_a.plan_hash == plan_b.plan_hash

    def test_hash_changes_when_incident_id_changes(self):
        plan_a = _make_plan(incident_id="inc-001")
        plan_b = _make_plan(incident_id="inc-002")
        assert plan_a.plan_hash != plan_b.plan_hash

    def test_hash_changes_when_action_type_changes(self):
        plan_a = _make_plan(action_type=ActionType.START_CONTAINER)
        plan_b = _make_plan(action_type=ActionType.RESTART_CONTAINER)
        assert plan_a.plan_hash != plan_b.plan_hash

    def test_hash_changes_when_operation_changes(self):
        plan_a = _make_plan(operation="start moodle-app via Docker Compose")
        plan_b = _make_plan(operation="restart moodle-app via Docker Compose")
        assert plan_a.plan_hash != plan_b.plan_hash

    def test_hash_changes_when_evidence_ids_change(self):
        plan_a = _make_plan(evidence_ids=["ev-001", "ev-002", "ev-003"])
        plan_b = _make_plan(evidence_ids=["ev-001", "ev-002", "ev-099"])
        assert plan_a.plan_hash != plan_b.plan_hash

    def test_hash_changes_when_rollback_changes(self):
        plan_a = _make_plan(rollback=_make_reversible_rollback(max_rollback_seconds=60))
        plan_b = _make_plan(rollback=_make_reversible_rollback(max_rollback_seconds=120))
        assert plan_a.plan_hash != plan_b.plan_hash

    def test_hash_changes_when_target_resource_changes(self):
        scope_a = _make_scope(affected_resource_ids=["moodle-app"])
        scope_b = _make_scope(affected_resource_ids=["postgres-db"])
        plan_a = _make_plan(target_resource_id="moodle-app", expected_scope=scope_a)
        plan_b = _make_plan(target_resource_id="postgres-db", expected_scope=scope_b)
        assert plan_a.plan_hash != plan_b.plan_hash

    def test_hash_stable_regardless_of_dict_key_order(self):
        """_compute_plan_hash uses sort_keys=True so key order must not matter."""
        payload_a = {"incident_id": "inc-001", "operation": "start", "z_field": "last"}
        payload_b = {"z_field": "last", "operation": "start", "incident_id": "inc-001"}
        assert _compute_plan_hash(payload_a) == _compute_plan_hash(payload_b)

    def test_hash_differs_for_different_key_order_values(self):
        """Different values should produce different hashes even if keys are same."""
        payload_a = {"incident_id": "inc-001"}
        payload_b = {"incident_id": "inc-002"}
        assert _compute_plan_hash(payload_a) != _compute_plan_hash(payload_b)


# ===========================================================================
# ActionPlan — safety constraints
# ===========================================================================


class TestActionPlanConstraints:
    def test_rejects_blocked_unrestricted_shell(self):
        with pytest.raises(ValueError, match="BLOCKED_UNRESTRICTED_SHELL"):
            _make_plan(action_type=ActionType.BLOCKED_UNRESTRICTED_SHELL)

    def test_rejects_caller_supplied_plan_hash(self):
        with pytest.raises(ValueError, match="plan_hash must not be supplied"):
            ActionPlan(
                incident_id="inc-001",
                action_type=ActionType.START_CONTAINER,
                target_resource_id="moodle-app",
                operation="start",
                evidence_ids=["ev-001"],
                expected_scope=_make_scope(),
                expected_outcome="ok",
                rollback=_make_reversible_rollback(),
                requested_by="planner_agent",
                created_at=_NOW,
                plan_hash="deadbeef" * 8,  # caller-supplied — must be rejected
            )

    def test_rejects_target_not_in_scope(self):
        scope = _make_scope(affected_resource_ids=["postgres-db"])
        with pytest.raises(ValueError, match="not listed in expected_scope"):
            _make_plan(target_resource_id="moodle-app", expected_scope=scope)

    def test_rejects_empty_evidence_ids(self):
        with pytest.raises(ValueError):
            _make_plan(evidence_ids=[])

    def test_accepts_irreversible_rollback(self):
        plan = _make_plan(
            rollback=IrreversibleRollback(
                reason="Security-group change cannot be programmatically reversed."
            )
        )
        assert isinstance(plan.rollback, IrreversibleRollback)
        assert len(plan.plan_hash) == 64

    def test_plan_is_immutable_after_construction(self):
        plan = _make_plan()
        with pytest.raises(Exception):  # frozen model
            plan.operation = "tampered"  # type: ignore[misc]


# ===========================================================================
# ReversibleRollback / IrreversibleRollback
# ===========================================================================


class TestRollbackModels:
    def test_reversible_requires_steps(self):
        with pytest.raises(ValueError):
            ReversibleRollback(steps=[], max_rollback_seconds=60)

    def test_reversible_requires_positive_duration(self):
        with pytest.raises(ValueError):
            ReversibleRollback(steps=["step"], max_rollback_seconds=0)

    def test_reversible_rejects_blank_step(self):
        with pytest.raises(ValueError):
            ReversibleRollback(steps=["  "], max_rollback_seconds=30)

    def test_irreversible_requires_reason(self):
        with pytest.raises(ValueError):
            IrreversibleRollback(reason="")

    def test_irreversible_requires_non_blank_reason(self):
        with pytest.raises(ValueError):
            IrreversibleRollback(reason="   ")


# ===========================================================================
# Resource
# ===========================================================================


class TestResource:
    def test_valid_resource(self):
        r = Resource(
            resource_id="moodle-app",
            name="Moodle Web",
            type="application",
            environment=Environment.STAGING,
            host="moodle-ec2-01",
            labels={"tier": "web", "team": "infra"},
        )
        assert r.resource_id == "moodle-app"
        assert r.environment is Environment.STAGING

    def test_rejects_non_string_label_value(self):
        with pytest.raises(ValueError):
            Resource(
                resource_id="moodle-app",
                name="Moodle Web",
                type="application",
                environment=Environment.STAGING,
                labels={"tier": 42},  # type: ignore[dict-item]
            )

    def test_rejects_blank_label_key(self):
        with pytest.raises(ValueError):
            Resource(
                resource_id="moodle-app",
                name="Moodle Web",
                type="application",
                environment=Environment.STAGING,
                labels={"  ": "value"},
            )

    def test_no_host_is_valid(self):
        r = Resource(
            resource_id="efs-volume",
            name="EFS Volume",
            type="volume",
            environment=Environment.STAGING,
        )
        assert r.host is None

    def test_immutable_after_construction(self):
        r = Resource(
            resource_id="moodle-app",
            name="Moodle Web",
            type="application",
            environment=Environment.STAGING,
        )
        with pytest.raises(Exception):
            r.name = "tampered"  # type: ignore[misc]


# ===========================================================================
# Incident
# ===========================================================================


class TestIncident:
    def test_default_status_is_open(self):
        inc = Incident(
            incident_id="inc-001",
            alert_fingerprint="fp-abc123",
            resource_ids=["moodle-app"],
        )
        assert inc.status is IncidentStatus.OPEN

    def test_evidence_ids_default_empty(self):
        inc = Incident(
            incident_id="inc-001",
            alert_fingerprint="fp-abc123",
            resource_ids=["moodle-app"],
        )
        assert inc.evidence_ids == []

    def test_rejects_empty_resource_ids(self):
        with pytest.raises(ValueError):
            Incident(
                incident_id="inc-001",
                alert_fingerprint="fp-abc123",
                resource_ids=[],
            )

    def test_diagnosis_id_starts_none(self):
        inc = Incident(
            incident_id="inc-001",
            alert_fingerprint="fp-abc123",
            resource_ids=["moodle-app"],
        )
        assert inc.diagnosis_id is None

    def test_round_trip_json(self):
        inc = Incident(
            incident_id="inc-001",
            alert_fingerprint="fp-abc123",
            resource_ids=["moodle-app", "postgres-db"],
            diagnosis_id="diag-001",
            evidence_ids=["ev-001"],
        )
        serialised = inc.model_dump(mode="json")
        restored = Incident.model_validate(serialised)
        assert restored.incident_id == inc.incident_id
        assert restored.status is inc.status

