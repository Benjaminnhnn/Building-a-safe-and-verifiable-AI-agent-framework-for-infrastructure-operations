from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.evidence_store import EvidenceConflictError, EvidenceIntegrityError, SQLiteEvidenceStore
from core.orchestrator import (
    SUCCESS_STAGE_ORDER,
    CheckpointOrchestrator,
    OrchestratorStage,
)
from core.replay import observations_from_scenario
from core.schema.audit import AuditEvent, StageAuditEvent
from core.schema.resource import Resource
from core.schema.scenario import ScenarioGroundTruth


def _resources() -> list[Resource]:
    data = json.loads(
        Path("evaluation/resources/moodle_resource_inventory.json").read_text(encoding="utf-8")
    )
    return [Resource(**item) for item in data["resources"]]


def _scenarios() -> list[ScenarioGroundTruth]:
    prefixes = ("DB-", "RES-", "NET-", "CON-", "SEC-")
    paths = sorted(
        path
        for path in Path("evaluation/ground_truth").glob("*.json")
        if path.name.startswith(prefixes)
    )
    return [ScenarioGroundTruth(**json.loads(path.read_text(encoding="utf-8"))) for path in paths]


def test_all_fifteen_scenarios_replay_in_stage_order(tmp_path) -> None:
    scenarios = _scenarios()
    assert len(scenarios) == 15
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        orchestrator = CheckpointOrchestrator(store)
        for scenario in scenarios:
            result = orchestrator.run_scenario(
                scenario,
                _resources(),
                observations=observations_from_scenario(scenario),
                run_id=f"run-{scenario.scenario_id.lower()}",
            )
            assert result.error is None
            assert result.simulated is True
            assert result.incident.resolved_by_verifier is False
            assert len(result.evidence) >= 3
            assert result.audit_complete is True
            assert result.audit_completeness == 1.0
            if result.incident.status.value == "escalated":
                assert result.action is None
                assert result.safety is None
                assert result.execution_result is None
                assert result.verification is None
                continue
            assert result.stages == (
                SUCCESS_STAGE_ORDER
                if result.safety.decision.value == "allow"
                else SUCCESS_STAGE_ORDER[:-1]
            )
            assert result.action is not None
            assert result.safety is not None
            if result.safety.decision.value == "allow":
                assert result.incident.status.value == "verified_dry_run"
                assert result.verification is not None
            else:
                assert result.incident.status.value == "gated"
                assert result.verification is None
                assert result.execution_result is not None
                assert not result.execution_result.success


def test_resume_from_checkpoint_does_not_duplicate_records(tmp_path) -> None:
    scenario = _scenarios()[0]
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        orchestrator = CheckpointOrchestrator(store)
        partial = orchestrator.run_scenario(
            scenario,
            _resources(),
            observations=observations_from_scenario(scenario),
            run_id="resume-run",
            stop_after=OrchestratorStage.DIAGNOSE,
        )
        assert partial.stages[-1] == OrchestratorStage.DIAGNOSE
        evidence_count = len(store.list_for_incident(partial.incident.incident_id))
        audit_count = len(partial.audit_events)

        observations = observations_from_scenario(scenario)
        resumed = orchestrator.run_scenario(
            scenario, _resources(), observations=observations, run_id="resume-run"
        )
        repeated = orchestrator.run_scenario(
            scenario, _resources(), observations=observations, run_id="resume-run"
        )

        assert resumed.stages == SUCCESS_STAGE_ORDER[:-1]
        assert repeated == resumed
        assert len(store.list_for_incident(resumed.incident.incident_id)) == evidence_count
        assert len(resumed.audit_events) == audit_count + 4
        assert resumed.audit_complete is True
        assert resumed.audit_completeness == 1.0
        assert len(store.list_checkpoints("resume-run")) == len(SUCCESS_STAGE_ORDER) - 1


def test_resume_rejects_reused_run_id_with_changed_observation(tmp_path) -> None:
    scenario = _scenarios()[0]
    observations = observations_from_scenario(scenario)
    with SQLiteEvidenceStore(tmp_path / "input-binding.db") as store:
        orchestrator = CheckpointOrchestrator(store)
        orchestrator.run_scenario(
            scenario,
            _resources(),
            observations=observations,
            run_id="input-bound-run",
            stop_after=OrchestratorStage.OBSERVE,
        )
        changed = [
            observations[0].model_copy(update={"summary": "changed observation"}),
            *observations[1:],
        ]

        with pytest.raises(EvidenceConflictError, match="reused with different inputs"):
            orchestrator.run_scenario(
                scenario,
                _resources(),
                observations=changed,
                run_id="input-bound-run",
            )


def test_transition_audit_is_append_only_and_verified_on_checkpoint_load(tmp_path) -> None:
    scenario = _scenarios()[0]
    database = tmp_path / "audit-ledger.db"
    with SQLiteEvidenceStore(database) as store:
        result = CheckpointOrchestrator(store).run_scenario(
            scenario,
            _resources(),
            observations=observations_from_scenario(scenario),
            run_id="audit-ledger-run",
            stop_after=OrchestratorStage.TRIAGE,
        )
        events = store.list_audit_events(result.run_id)
        assert len(events) == len(result.audit_events) == 3
        assert [event.stage for event in result.audit_events if isinstance(event, StageAuditEvent)] == [
            "observe",
            "triage",
        ]
        assert result.audit_complete is True
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._connection.execute(
                "UPDATE incident_audit_events SET payload_json = '{}' WHERE run_id = ?",
                (result.run_id,),
            )

        store._connection.execute("DROP TRIGGER audit_event_no_update")
        store._connection.execute(
            "UPDATE incident_audit_events SET payload_json = '{}' WHERE run_id = ?",
            (result.run_id,),
        )
        store._connection.commit()
        with pytest.raises(EvidenceIntegrityError, match="digest or identity"):
            store.load_latest_checkpoint(result.run_id)


def test_resume_from_gate_request_stops_before_verification_when_policy_denies(tmp_path) -> None:
    scenario = _scenarios()[0]
    observations = observations_from_scenario(scenario)
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        orchestrator = CheckpointOrchestrator(store)
        paused = orchestrator.run_scenario(
            scenario,
            _resources(),
            observations=observations,
            run_id="resume-gate-run",
            stop_after=OrchestratorStage.GATE_REQUEST,
        )
        assert paused.stages[-1] == OrchestratorStage.GATE_REQUEST

        resumed = orchestrator.run_scenario(
            scenario,
            _resources(),
            observations=observations,
            run_id="resume-gate-run",
        )

        assert resumed.stages == SUCCESS_STAGE_ORDER[:-1]
        assert resumed.stages[-1] == OrchestratorStage.EXECUTE
        assert resumed.incident.status.value == "gated"
        assert resumed.incident.resolved_by_verifier is False
        assert resumed.verification is None
        assert resumed.execution_result is not None
        assert resumed.execution_result.error == "pipeline_denied"
        assert len(store.list_for_incident(resumed.incident.incident_id)) == len(resumed.evidence)


def test_malformed_planner_output_fails_closed_and_is_audited(tmp_path) -> None:
    scenario = _scenarios()[0]

    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        orchestrator = CheckpointOrchestrator(store)

        def malformed_plan(*args, **kwargs):
            raise ValueError("action outside typed catalog: raw_shell")

        orchestrator.planner_agent.plan = malformed_plan
        result = orchestrator.run_scenario(
            scenario,
            _resources(),
            observations=observations_from_scenario(scenario),
            run_id="failed-run",
        )

        assert result.stages[-1] == OrchestratorStage.FAILED
        assert result.incident.status == "failed"
        assert "outside typed catalog" in result.error
        transition_events = [event for event in result.audit_events if isinstance(event, AuditEvent)]
        stage_events = [event for event in result.audit_events if isinstance(event, StageAuditEvent)]
        assert transition_events[-1].actor == "orchestrator"
        assert "Malformed stage output" in transition_events[-1].reason
        assert stage_events[-1].stage == "failed"
        assert stage_events[-1].outcome == "failed"
        assert result.audit_complete is True


def test_concurrent_run_is_rejected_while_another_worker_owns_checkpoint(tmp_path) -> None:
    scenario = _scenarios()[0]
    observations = observations_from_scenario(scenario)
    database = tmp_path / "concurrent-run.db"
    entered_observe = threading.Event()
    release_observe = threading.Event()
    worker_errors: list[Exception] = []

    def worker() -> None:
        try:
            with SQLiteEvidenceStore(database) as store:
                orchestrator = CheckpointOrchestrator(store)
                original_observe = orchestrator._observe

                def blocked_observe(*args, **kwargs):
                    entered_observe.set()
                    assert release_observe.wait(timeout=5)
                    return original_observe(*args, **kwargs)

                orchestrator._observe = blocked_observe
                orchestrator.run_scenario(
                    scenario,
                    _resources(),
                    observations=observations,
                    run_id="shared-concurrent-run",
                )
        except Exception as exc:
            worker_errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered_observe.wait(timeout=5)
    try:
        with SQLiteEvidenceStore(database) as store:
            with pytest.raises(EvidenceConflictError, match="already owned"):
                CheckpointOrchestrator(store).run_scenario(
                    scenario,
                    _resources(),
                    observations=observations,
                    run_id="shared-concurrent-run",
                )
    finally:
        release_observe.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert worker_errors == []
    with SQLiteEvidenceStore(database) as store:
        checkpoints = store.list_checkpoints("shared-concurrent-run")
        assert len(checkpoints) == len(SUCCESS_STAGE_ORDER) - 1
        assert checkpoints[-1]["stage"] == OrchestratorStage.EXECUTE.value


def test_expired_worker_cannot_overwrite_checkpoint_after_lease_takeover(tmp_path) -> None:
    with SQLiteEvidenceStore(tmp_path / "fenced-checkpoint.db") as store:
        assert store.claim_checkpoint_run(run_id="fenced-run", owner_token="old-owner")
        store._connection.execute(
            "UPDATE checkpoint_run_claims SET lease_expires_at = ? WHERE run_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), "fenced-run"),
        )
        store._connection.commit()
        assert store.claim_checkpoint_run(run_id="fenced-run", owner_token="new-owner")

        checkpoint = {
            "run_id": "fenced-run",
            "incident_id": "incident-1",
            "stage": "observe",
            "sequence_number": 0,
            "updated_at": datetime.now(timezone.utc),
        }
        with pytest.raises(EvidenceConflictError, match="claim was lost"):
            store.save_claimed_checkpoint(
                **checkpoint,
                owner_token="old-owner",
                payload={"owner": "old"},
            )

        store.save_claimed_checkpoint(
            **checkpoint,
            owner_token="new-owner",
            payload={"owner": "new"},
        )
        assert store.load_latest_checkpoint("fenced-run")["payload"] == {"owner": "new"}
