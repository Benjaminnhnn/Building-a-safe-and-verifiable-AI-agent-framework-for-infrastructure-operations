"""Sprint 2 contract tests for the replayable Moodle incident workflow."""

from __future__ import annotations

from pathlib import Path

import pytest
from core.moodle_pipeline import (
    SCENARIOS,
    GateDecision,
    MoodleIncidentPipeline,
    MoodleSafetyGate,
    PipelineError,
    TypedAction,
    load_moodle_catalog,
)


def _event(scenario_id: str) -> dict:
    return {
        "schema_version": "2.0",
        "event_id": f"event-{scenario_id}",
        "fingerprint": f"fingerprint-{scenario_id}",
        "source": "synthetic",
        "event_type": "service_health_failed",
        "observed_at": "2026-09-21T00:00:00Z",
        "labels": {"alertname": "MoodleSyntheticTransactionFailed", "scenario_id": scenario_id},
    }


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_each_ground_truth_has_one_replayable_resolved_incident(tmp_path: Path, scenario_id: str) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    report = pipeline.process(_event(scenario_id), scenario_id=scenario_id)
    assert report["state"] == "RESOLVED"
    assert report["execution"]["status"] == "dry-run"
    assert report["verification"]["status"] == "passed"
    assert report["verification"]["authority"] == "independent_verifier"
    assert set(report["verification"]) >= {"allowed_probes", "forbidden_probes", "related_probes"}
    assert len(report["evidence_refs"]) >= 5
    assert len(report["audit_refs"]) >= 5

    replay = pipeline.process(_event(scenario_id), scenario_id=scenario_id)
    assert replay["replayed"] is True
    assert len(pipeline.incidents) == 1
    assert pipeline.evidence.refs(report["incident_id"]) == report["evidence_refs"]


def test_malformed_or_out_of_scope_action_is_rejected() -> None:
    catalog = load_moodle_catalog()
    gate = MoodleSafetyGate(catalog)
    invalid = TypedAction(
        scenario_id="DB-01",
        action="delete_database",
        target="staging_moodle_nodes",
        environment="production",
        mode="execute",
        idempotency_key="test",
    )
    with pytest.raises(PipelineError):
        gate.validate_action(invalid)
    decision = gate.decide(invalid, ["a", "b", "c"], 0.99)
    assert decision.decision == GateDecision.DENY


def test_safety_gate_requires_evidence_and_approval_at_medium_confidence(tmp_path: Path) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    ground_truth = pipeline.catalog["CON-01"]
    remediation = ground_truth["allowed_remediation"][0]
    action = TypedAction("CON-01", remediation["action"], remediation["target"], idempotency_key="test")
    assert pipeline.gate.decide(action, ["one", "two"], 0.99).decision == GateDecision.DENY
    assert pipeline.gate.decide(action, ["one", "two", "three"], 0.70).decision == GateDecision.REQUIRE_APPROVAL


def test_pipeline_never_accepts_sensitive_evidence(tmp_path: Path) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    event = _event("DB-01")
    event["labels"]["password"] = "not-a-real-password"
    with pytest.raises(PipelineError):
        pipeline.process(event, scenario_id="DB-01")


def test_evaluation_pipeline_cannot_execute_live_remediation(tmp_path: Path) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    with pytest.raises(PipelineError, match="evaluation-only"):
        pipeline.process(
            _event("DB-01"),
            scenario_id="DB-01",
            mode="execute",
            live_verify=True,
        )
