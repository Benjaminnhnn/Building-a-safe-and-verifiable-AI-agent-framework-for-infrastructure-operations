from __future__ import annotations

import json
from pathlib import Path

from core.evidence_store import SQLiteEvidenceStore
from core.orchestrator import (
    SUCCESS_STAGE_ORDER,
    CheckpointOrchestrator,
    OrchestratorStage,
)
from core.replay import observations_from_scenario
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
            assert result.stages == SUCCESS_STAGE_ORDER
            assert result.stages[-1] == OrchestratorStage.VERIFY
            assert result.simulated is True
            assert result.action is not None
            assert result.safety is not None
            assert result.incident.status == "resolved"
            assert result.incident.resolved_by_verifier is True
            assert len(result.evidence) >= 3


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

        assert resumed.stages == SUCCESS_STAGE_ORDER
        assert repeated == resumed
        assert len(store.list_for_incident(resumed.incident.incident_id)) == evidence_count
        assert len(resumed.audit_events) == audit_count + 4
        assert len(store.list_checkpoints("resume-run")) == len(SUCCESS_STAGE_ORDER)


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
        assert result.audit_events[-1].actor == "orchestrator"
        assert "Malformed stage output" in result.audit_events[-1].reason
