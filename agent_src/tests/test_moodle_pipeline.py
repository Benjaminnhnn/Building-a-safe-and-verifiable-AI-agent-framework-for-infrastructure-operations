"""Sprint 2 contract tests for the replayable Moodle incident workflow."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.event_schema import normalize_alert
from core.moodle_pipeline import (
    GateDecision,
    HumanApproval,
    MoodleIncidentPipeline,
    MoodleSafetyGate,
    PipelineError,
    SCENARIOS,
    TypedAction,
    load_moodle_catalog,
)


def _event(scenario_id: str) -> dict:
    return normalize_alert(
        {
            "status": "firing",
            "fingerprint": f"fingerprint-{scenario_id}",
            "startsAt": "2026-09-21T00:00:00Z",
            "labels": {
                "alertname": "MoodleSyntheticTransactionFailed",
                "scenario_id": scenario_id,
                "environment": "staging",
                "severity": "critical",
            },
            "annotations": {"summary": f"Synthetic failure for {scenario_id}"},
        },
        correlation_id=f"event-{scenario_id}",
        received_at="2026-09-21T00:00:01Z",
    )


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_each_ground_truth_has_one_replayable_dry_run_incident(tmp_path: Path, scenario_id: str) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    report = pipeline.process(_event(scenario_id), scenario_id=scenario_id)
    assert report["state"] == "VERIFIED_DRY_RUN"
    assert report["execution"]["status"] == "dry-run"
    assert report["verification"]["status"] == "not_run"
    assert report["verification"]["resolution_eligible"] is False
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
    assert pipeline.gate.decide(action, ["one", "two", "three"], float("nan")).decision == GateDecision.DENY


def test_pipeline_requires_signed_action_bound_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    scenario = pipeline.catalog["CON-01"]
    remediation = scenario["allowed_remediation"][0]
    action = TypedAction("CON-01", remediation["action"], remediation["target"], idempotency_key="test-approval")
    action_hash = hashlib.sha256(json.dumps(asdict(action), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    signed_payload = {"action_sha256": action_hash, "approver_id": "reviewer-1", "expires_at": expires_at}
    secret = "unit-test-key-" + ("x" * 32)
    monkeypatch.setenv("MOODLE_APPROVAL_HMAC_KEY", secret)
    monkeypatch.setenv("MOODLE_APPROVER_IDS", "reviewer-1")
    signature = hmac.new(secret.encode(), json.dumps(signed_payload, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    approval = HumanApproval(**signed_payload, signature=signature)

    assert pipeline.gate.validate_approval(action, approval) == (True, "valid action-bound approval")
    tampered = TypedAction("CON-01", remediation["action"], "production", idempotency_key="test-approval")
    assert pipeline.gate.validate_approval(tampered, approval)[0] is False


def test_replay_cannot_resolve_incident_without_live_verification(tmp_path: Path) -> None:
    report = MoodleIncidentPipeline(tmp_path).process(_event("DB-01"), scenario_id="DB-01")
    assert report["state"] == "VERIFIED_DRY_RUN"
    assert report["verification"]["resolution_eligible"] is False
    assert report["verification"]["stability_window_seconds"] == 0


def test_verifier_rejects_health_only_and_short_stability(tmp_path: Path) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    contract = pipeline.catalog["DB-01"]["communication_contract"]
    runtime = {"status": "passed", "mode": "live", "communication_contract": None, "stability_observation": None}
    result = pipeline.verifier.verify(contract, live=True, runtime=runtime)
    assert result["status"] == "blocked"
    assert result["resolution_eligible"] is False


def test_verifier_requires_every_probe_and_120_second_stability(tmp_path: Path) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    contract = pipeline.catalog["DB-01"]["communication_contract"]
    start = datetime.now(timezone.utc) - timedelta(seconds=120)

    def runtime(seconds: int) -> dict:
        return {
            "status": "passed",
            "mode": "live",
            "communication_contract": {
                "allowed": {probe: "passed" for probe in contract["allowed"]},
                "forbidden": {probe: "blocked" for probe in contract["forbidden"]},
                "related": {probe: "passed" for probe in contract["related"]},
            },
            "stability_observation": {
                "status": "passed",
                "observations": [
                    {"observed_at": start.isoformat(), "status": "passed"},
                    {"observed_at": (start + timedelta(seconds=seconds)).isoformat(), "status": "passed"},
                ],
            },
        }

    accepted = pipeline.verifier.verify(contract, live=True, runtime=runtime(120))
    rejected = pipeline.verifier.verify(contract, live=True, runtime=runtime(119))
    assert accepted["status"] == "passed"
    assert accepted["resolution_eligible"] is True
    assert rejected["status"] == "failed"
    assert rejected["resolution_eligible"] is False


def test_pipeline_rejects_malformed_normalized_alert(tmp_path: Path) -> None:
    event = _event("DB-01")
    del event["severity"]
    with pytest.raises(PipelineError, match="malformed normalized alert"):
        MoodleIncidentPipeline(tmp_path).process(event, scenario_id="DB-01")


def test_resolved_alert_cannot_start_remediation(tmp_path: Path) -> None:
    event = normalize_alert(
        {
            "status": "resolved",
            "fingerprint": "resolved-db-01",
            "labels": {"alertname": "PostgreSQLDown", "environment": "staging", "severity": "critical"},
            "annotations": {"summary": "Database alert resolved"},
        },
        correlation_id="resolved-test",
    )
    with pytest.raises(PipelineError, match="only firing"):
        MoodleIncidentPipeline(tmp_path).process(event, scenario_id="DB-01")


def test_medium_confidence_without_valid_approval_never_executes(tmp_path: Path) -> None:
    report = MoodleIncidentPipeline(tmp_path).process(
        _event("CON-01"), scenario_id="CON-01", confidence=0.70, mode="dry-run"
    )
    assert report["state"] == "AWAITING_APPROVAL"
    assert report["gate"]["decision"] == GateDecision.REQUIRE_APPROVAL
    assert report["execution"] is None


def test_pipeline_never_accepts_sensitive_evidence(tmp_path: Path) -> None:
    pipeline = MoodleIncidentPipeline(tmp_path)
    event = _event("DB-01")
    event["labels"]["password"] = "not-a-real-password"
    with pytest.raises(PipelineError):
        pipeline.process(event, scenario_id="DB-01")
