from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.unified_core import (
    AgentMessage,
    Incident,
    IncidentState,
    ObserverAgent,
    SQLiteEvidenceStore,
    SequentialOrchestrator,
    TypedAction,
    resolve_verified,
    transition,
    transition_action,
)
from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult


def test_incident_transition_guard_and_verifier_authority() -> None:
    incident = Incident(incident_id="i1", fingerprint="f1", environment="staging", resource_ids=["moodle"])
    with pytest.raises(ValueError, match="resolve_verified"):
        transition(incident.model_copy(update={"state": IncidentState.VERIFYING}), IncidentState.RESOLVED, actor="orchestrator")
    with pytest.raises(ValueError, match="resolve_verified"):
        transition(incident.model_copy(update={"state": IncidentState.VERIFYING}), IncidentState.RESOLVED, actor="verifier")
    checked = datetime.now(timezone.utc)
    replay_result = resolve_verified(incident.model_copy(update={"state": IncidentState.VERIFYING}), VerificationResult(
            verification_id="v1", incident_id="i1", health_passed=True,
            communication_contract_passed=True, stability_seconds=120,
            stability_observations=[
                StabilityObservation(observed_at=checked - timedelta(seconds=120), healthy=True, simulated=False),
                StabilityObservation(observed_at=checked - timedelta(seconds=60), healthy=True, simulated=False),
                StabilityObservation(observed_at=checked, healthy=True, simulated=False),
            ],
            allowed_probes=[ProbeResult(name="moodle", passed=True, simulated=False, details="ok")],
            verdict="resolved", resolution_eligible=True, simulated=True,
        ))
    assert replay_result.state == IncidentState.VERIFIED_DRY_RUN


def test_incident_state_cannot_be_assigned_directly() -> None:
    incident = Incident(
        incident_id="i1",
        fingerprint="f1",
        environment="staging",
        resource_ids=["moodle"],
    )

    with pytest.raises(ValueError):
        incident.state = IncidentState.RESOLVED


def test_verified_resolution_requires_probe_and_stability_evidence() -> None:
    checked = datetime.now(timezone.utc)
    incident = Incident(incident_id="i1", fingerprint="f1", environment="staging", resource_ids=["moodle"], state=IncidentState.VERIFYING)
    verification = VerificationResult(
        verification_id="v1", incident_id="i1", health_passed=True,
        communication_contract_passed=True, stability_seconds=120,
        stability_observations=[
            StabilityObservation(observed_at=checked - timedelta(seconds=120), healthy=True, simulated=False),
            StabilityObservation(observed_at=checked - timedelta(seconds=60), healthy=True, simulated=False),
            StabilityObservation(observed_at=checked, healthy=True, simulated=False),
        ],
        allowed_probes=[ProbeResult(name="moodle", passed=True, simulated=False, details="ok")],
        forbidden_probes=[ProbeResult(name="public_db", passed=True, simulated=False, details="blocked")],
        related_probes=[ProbeResult(name="monitoring", passed=True, simulated=False, details="ok")],
        verdict="resolved", resolution_eligible=True, simulated=False,
    )
    assert resolve_verified(incident, verification).state == IncidentState.RESOLVED


def test_legacy_resolution_revalidates_verifier_model_copy() -> None:
    incident = Incident(
        incident_id="i-copy",
        fingerprint="f-copy",
        environment="staging",
        resource_ids=["moodle"],
        state=IncidentState.VERIFYING,
    )
    checked = datetime.now(timezone.utc)
    verification = VerificationResult(
        verification_id="v-copy",
        incident_id="i-copy",
        health_passed=True,
        communication_contract_passed=True,
        stability_seconds=120,
        stability_observations=[
            StabilityObservation(observed_at=checked - timedelta(seconds=120), healthy=True),
            StabilityObservation(observed_at=checked, healthy=True),
        ],
        allowed_probes=[ProbeResult(name="moodle", passed=True, details="ok")],
        verdict="resolved",
        resolution_eligible=True,
    ).model_copy(update={"health_passed": 1})

    with pytest.raises(ValueError, match="schema validation"):
        resolve_verified(incident, verification)
    assert incident.state == IncidentState.VERIFYING


def test_live_state_rejects_simulated_nested_probe_evidence() -> None:
    checked = datetime.now(timezone.utc)
    incident = Incident(incident_id="i1", fingerprint="f1", environment="staging", resource_ids=["moodle"], state=IncidentState.VERIFYING)
    verification = VerificationResult(
        verification_id="v1", incident_id="i1", health_passed=True,
        communication_contract_passed=True, stability_seconds=120,
        stability_observations=[
            StabilityObservation(observed_at=checked - timedelta(seconds=120), healthy=True, simulated=False),
            StabilityObservation(observed_at=checked - timedelta(seconds=60), healthy=True, simulated=False),
            StabilityObservation(observed_at=checked, healthy=True, simulated=False),
        ],
        allowed_probes=[ProbeResult(name="moodle", passed=True, details="fixture")],
        forbidden_probes=[ProbeResult(name="public_db", passed=True, simulated=False, details="blocked")],
        related_probes=[ProbeResult(name="monitoring", passed=True, simulated=False, details="ok")],
        verdict="resolved", resolution_eligible=True, simulated=False,
    )
    with pytest.raises(ValueError, match="verifier evidence"):
        resolve_verified(incident, verification)


def test_action_lifecycle_rejects_skipping_gate_and_terminal_reuse() -> None:
    action = TypedAction(action_id="a1", incident_id="i1", action="restart", target_resource_id="moodle", environment="staging", evidence_ids=["ev1"], idempotency_key="a1")
    with pytest.raises(ValueError, match="invalid action lifecycle"):
        transition_action(action, "RUNNING")
    running = transition_action(transition_action(transition_action(action, "GATED"), "APPROVED"), "RUNNING")
    succeeded = transition_action(running, "SUCCEEDED")
    with pytest.raises(ValueError, match="invalid action lifecycle"):
        transition_action(succeeded, "RUNNING")


def test_evidence_is_queryable_hash_addressed_and_append_only(tmp_path) -> None:
    store = SQLiteEvidenceStore(tmp_path / "evidence.db")
    evidence = store.append(incident_id="i1", source="prometheus", resource_id="moodle", kind="metric", payload={"value": 7})
    found = store.query(incident_id="i1", resource_id="moodle")
    assert found == [evidence]
    assert len(evidence.sha256) == 64
    with store._connect() as db, pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE evidence SET kind='changed' WHERE evidence_id=?", (evidence.evidence_id,))


def test_equal_timestamp_evidence_query_preserves_sqlite_append_order(tmp_path) -> None:
    store = SQLiteEvidenceStore(tmp_path / "evidence.db")
    timestamp = datetime(2026, 10, 5, tzinfo=timezone.utc).isoformat()
    with store._connect() as db:
        for evidence_id, marker in (("ev-z-first", "first"), ("ev-a-second", "second")):
            db.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, "incident-1", "test", timestamp, "moodle", "transition", f'{{"marker":"{marker}"}}', "0" * 64),
            )

    rows = store.query(incident_id="incident-1")

    assert [row.payload["marker"] for row in rows] == ["first", "second"]


def test_dependency_graph_and_rag_provenance(tmp_path) -> None:
    store = SQLiteEvidenceStore(tmp_path / "evidence.db")
    store.add_dependency("moodle", "postgres", "connects_to")
    assert store.dependencies("moodle") == [{"source_id": "moodle", "target_id": "postgres", "relation": "connects_to"}]
    provenance = store.rag_provenance(source="runbook.md", source_type="runbook", observed_at=datetime.now(timezone.utc), content="restart safely", resource_id="moodle")
    assert set(provenance) == {"source", "source_type", "observed_at", "resource_id", "sha256"}
    assert len(provenance["sha256"]) == 64


def test_observer_groups_duplicate_alerts_without_appending_them(tmp_path) -> None:
    store = SQLiteEvidenceStore(tmp_path / "evidence.db")
    observer = ObserverAgent(store)
    event = {"fingerprint": "same-alert", "event_type": "service_health_failed"}
    first = observer.observe(event, resource_id="moodle")
    repeated = [observer.observe(event, resource_id="moodle") for _ in range(99)]
    assert first["duplicate"] is False
    assert all(item["duplicate"] for item in repeated)
    assert len(store.query(kind="alert_observation")) == 1
    assert (100 - len(store.query(kind="alert_observation"))) / 100 >= 0.99


def test_orchestrator_retries_and_resumes_successful_checkpoints(tmp_path) -> None:
    store = SQLiteEvidenceStore(tmp_path / "evidence.db")
    calls = {"observer": 0}

    def flaky(_context):
        calls["observer"] += 1
        if calls["observer"] == 1:
            raise RuntimeError("temporary")
        return {"group": "g1"}

    handlers = {stage: (flaky if stage == "observer" else lambda _c, s=stage: {"stage": s}) for stage in SequentialOrchestrator.STAGES}
    runner = SequentialOrchestrator(store, max_attempts=2)
    first = runner.run(incident_id="i1", handlers=handlers, context={"evidence_refs": ["ev-1"]})
    assert [m.stage for m in first] == list(SequentialOrchestrator.STAGES)
    assert calls["observer"] == 2
    resumed = runner.run(incident_id="i1", handlers=handlers, context={"evidence_refs": ["ev-1"]})
    assert all(m.status == "ok" for m in resumed)
    assert calls["observer"] == 2
    assert AgentMessage.model_validate(resumed[0].model_dump(mode="json")).status == "ok"


def test_orchestrator_escalates_when_required_stage_missing(tmp_path) -> None:
    messages = SequentialOrchestrator(SQLiteEvidenceStore(tmp_path / "evidence.db")).run(incident_id="i2", handlers={}, context={})
    assert len(messages) == 1
    assert messages[0].status == "escalate"
