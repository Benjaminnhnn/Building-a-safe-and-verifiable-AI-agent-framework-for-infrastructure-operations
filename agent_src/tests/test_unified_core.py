from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.unified_core import (
    AgentMessage,
    Incident,
    IncidentState,
    ObserverAgent,
    SQLiteEvidenceStore,
    SequentialOrchestrator,
    TypedAction,
    transition,
    transition_action,
)


def test_incident_transition_guard_and_verifier_authority() -> None:
    incident = Incident(incident_id="i1", fingerprint="f1", environment="staging", resource_ids=["moodle"])
    with pytest.raises(ValueError, match="only the independent verifier"):
        transition(incident.model_copy(update={"state": IncidentState.VERIFYING}), IncidentState.RESOLVED, actor="orchestrator")
    with pytest.raises(ValueError, match="invalid incident transition"):
        transition(incident, IncidentState.RESOLVED, actor="verifier")


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
