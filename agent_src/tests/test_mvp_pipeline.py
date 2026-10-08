from __future__ import annotations

import json
from pathlib import Path

from core.mvp_pipeline import (
    IncidentCore,
    _load_resource_graph,
    collect_fixture_evidence,
    diagnose,
    evaluate_safety,
    replay_scenario,
)
from core.schema.action import RollbackPlan
from core.schema.common import ActionDecision, ActionType, Environment
from core.schema.scenario import ScenarioGroundTruth


def _moodle_scenarios() -> list[Path]:
    return sorted(Path("evaluation/ground_truth/moodle").glob("*.json"))


def test_replay_pipeline_runs_five_moodle_scenarios() -> None:
    paths = _moodle_scenarios()
    assert len(paths) >= 5

    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = replay_scenario(data)

        assert result["incident"].resolved_by_verifier is False
        assert len(result["evidence"]) == len(data["observed_signals"])
        assert result["execution"]["dry_run"] is True
        if not result["diagnosis"].ready_for_planning or result["action"] is None:
            assert result["incident"].status.value == "escalated"
            assert result["execution"]["executed"] is False
            assert result["verification"] is None
        elif result["safety"].decision == ActionDecision.ALLOW:
            assert result["incident"].status.value == "verified_dry_run"
            assert result["execution"]["executed"] is True
            assert result["verification"] is not None
        else:
            assert result["incident"].status.value in {"gated", "escalated"}
            assert result["execution"]["executed"] is False
            assert result["verification"] is None

        assert result["timeline"]["t_detect"]
        assert result["timeline"]["t_incident"]
        assert result["timeline"]["t_diagnose"]


def test_incident_core_deduplicates_same_scenario() -> None:
    data = json.loads(Path("evaluation/ground_truth/moodle/CON-01.json").read_text(encoding="utf-8"))
    scenario = ScenarioGroundTruth(**data)
    core = IncidentCore()

    first = core.get_or_create(scenario)
    second = core.get_or_create(scenario)

    assert first.incident_id == second.incident_id
    assert first.fingerprint == second.fingerprint


def test_safety_gate_uses_catalog_not_ground_truth_for_authorization() -> None:
    data = json.loads(
        Path("evaluation/ground_truth/moodle/CON-01.json").read_text(
            encoding="utf-8"
        )
    )
    replay = replay_scenario(data)
    action = replay["action"].model_copy(
        update={
            "action_type": ActionType.READ_HEALTH,
            "target_resource_id": "moodle-web-endpoint",
            "parameters": {"catalog_action_id": "probe_moodle_http_health"},
            "reversible": True,
            "requires_approval": False,
            "rollback_plan": RollbackPlan(available=False),
        }
    )
    allowed = evaluate_safety(action, replay["diagnosis"])
    assert allowed.decision == ActionDecision.ALLOW
    denied = evaluate_safety(
        action.model_copy(update={"environment": Environment.PRODUCTION}),
        replay["diagnosis"],
    )

    assert denied.decision == ActionDecision.DENY
    assert denied.reasons == ["Action Catalog denies this role or environment"]
    assert "scenario" not in __import__("inspect").signature(evaluate_safety).parameters


def test_mvp_agents_ignore_ground_truth_answers_and_remediation_labels() -> None:
    data = json.loads(
        Path("evaluation/ground_truth/moodle/DB-01.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = replay_scenario(data)
    changed = json.loads(json.dumps(data))
    changed["expected_root_cause"]["cause"] = "oracle answer must not be read"
    changed["allowed_remediation"] = [{"action": "unrestricted_shell", "target": "*"}]
    changed["forbidden_actions"] = []
    candidate = replay_scenario(changed)

    assert candidate["diagnosis"].top_hypothesis == baseline[
        "diagnosis"
    ].top_hypothesis
    assert candidate["action"] == baseline["action"]
    assert candidate["safety"] == baseline["safety"]


def test_mvp_diagnosis_does_not_pad_missing_fixture_evidence() -> None:
    data = json.loads(
        Path("evaluation/ground_truth/DB-01-postgresql-stopped.json").read_text(
            encoding="utf-8"
        )
    )
    data["observed_signals"] = data["observed_signals"][:2]
    scenario = ScenarioGroundTruth(**data)
    incident = IncidentCore().get_or_create(scenario)
    evidence = collect_fixture_evidence(scenario, incident)

    diagnosis = diagnose(incident, evidence, _load_resource_graph())

    assert len(evidence) == 2
    assert diagnosis.ready_for_planning is False
    assert diagnosis.top_hypothesis is None
