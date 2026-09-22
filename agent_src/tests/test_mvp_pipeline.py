from __future__ import annotations

import json
from pathlib import Path

from core.mvp_pipeline import IncidentCore, evaluate_safety, replay_scenario
from core.schema.action import RollbackPlan
from core.schema.common import ActionDecision, Environment
from core.schema.scenario import ScenarioGroundTruth


def _moodle_scenarios() -> list[Path]:
    prefixes = ("DB-", "RES-", "NET-", "CON-", "SEC-")
    return sorted(path for path in Path("evaluation/ground_truth").glob("*.json") if path.name.startswith(prefixes))


def test_replay_pipeline_runs_five_moodle_scenarios() -> None:
    paths = _moodle_scenarios()
    assert len(paths) >= 5

    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = replay_scenario(data)

        assert result["incident"].resolved_by_verifier is True
        assert len(result["evidence"]) >= 3
        assert result["diagnosis"].ready_for_planning is True
        assert result["safety"].decision == ActionDecision.ALLOW
        assert result["execution"]["dry_run"] is True
        assert result["verification"].communication_contract_passed is True
        assert result["verification"].stability_seconds >= 120

        timeline = result["timeline"]
        for key in [
            "t_detect",
            "t_incident",
            "t_plan",
            "t_gate",
            "t_execute_start",
            "t_execute_end",
            "t_verify",
            "t_resolved",
        ]:
            assert timeline[key]


def test_incident_core_deduplicates_same_scenario() -> None:
    data = json.loads(Path("evaluation/ground_truth/DB-01-postgresql-stopped.json").read_text(encoding="utf-8"))
    scenario = ScenarioGroundTruth(**data)
    core = IncidentCore()

    first = core.get_or_create(scenario)
    second = core.get_or_create(scenario)

    assert first.incident_id == second.incident_id
    assert first.fingerprint == second.fingerprint


def test_deny_precedence_cannot_be_overwritten_by_approval_rules() -> None:
    data = json.loads(
        Path("evaluation/ground_truth/DB-01-postgresql-stopped.json").read_text(
            encoding="utf-8"
        )
    )
    scenario = ScenarioGroundTruth(**data)
    replay = replay_scenario(data)
    action = replay["action"].model_copy(
        update={
            "environment": Environment.PRODUCTION,
            "rollback_plan": RollbackPlan(available=False),
        }
    )
    scenario.forbidden_actions.append(action.action_type.value)

    decision = evaluate_safety(action, scenario)

    assert decision.decision == ActionDecision.DENY
    assert "action is forbidden by ground truth" in decision.reasons
    assert "non-staging environment requires approval" in decision.reasons
