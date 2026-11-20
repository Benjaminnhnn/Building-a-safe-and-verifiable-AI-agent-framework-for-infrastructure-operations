"""Plan, behavior-policy and outcome-classification tests for the lab runner (no Docker needed)."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "lab/moodle-local", ROOT / "evaluation/aihs"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import action_ladder as ladder  # noqa: E402
import lab_runner as runner  # noqa: E402


def test_plan_is_deterministic_and_only_uses_listed_actions():
    first = runner.build_plan("c", 120, 7)
    assert first == runner.build_plan("c", 120, 7)
    assert first != runner.build_plan("c", 120, 8)
    for item in first:
        assert item["planned_action"] in ladder.PREDICTOR_CLASSES
        assert item["episode_id"].startswith("ep-") and not any(s in item["episode_id"] for s in ("DB", "NET", "RES"))
        assert set(item["faults"]) <= set(runner.lab_faults.LAB_SCENARIOS)


def test_plan_is_balanced_and_contains_exploration_and_ambiguity():
    plan = runner.build_plan("balance", 420, 1)
    scenarios = Counter(p["faults"][0] for p in plan)
    assert max(scenarios.values()) - min(scenarios.values()) <= 1
    actions = Counter(p["planned_action"] for p in plan)
    assert set(actions) == set(ladder.PREDICTOR_CLASSES)
    assert all(count >= 20 for count in actions.values()), actions
    assert 0.15 < sum(p["kind"] == "ambiguous_pair" for p in plan) / len(plan) < 0.35
    assert sum(p["exploration"] for p in plan) > 100
    assert any(p["transient"] for p in plan) and any(p["background_load"]["node"] for p in plan)
    # every executable catalog action is tried both as the matching and as a non-matching choice
    for action in ladder.CATALOG_ACTIONS:
        assert any(p["planned_action"] == action and not p["exploration"] for p in plan)
        assert any(p["planned_action"] == action and p["exploration"] for p in plan)


def snapshot(cutoff="2026-11-15T12:00:05.000Z"):
    return {"detected_at": "2026-11-15T12:00:01.000Z", "evidence_cutoff_at": cutoff, "capture_finished_at": cutoff,
            "features_sha256": "a" * 64, "evidence_bundle_sha256": "b" * 64}


def test_policy_enforces_cutoff_before_prediction_and_the_action_space():
    item = {"planned_action": "remove_scoped_db_reject"}
    ok = runner.choose_action(snapshot(), {"x": 1.0}, item, "2026-11-15T12:00:06.000Z")
    assert ok["action_id"] == "remove_scoped_db_reject" and ok["predicted_at"] > ok["evidence_cutoff_at"]
    with pytest.raises(Exception):
        runner.choose_action(snapshot(), {}, item, "2026-11-15T12:00:04.000Z")          # decision before the cutoff
    with pytest.raises(ValueError):
        runner.choose_action(snapshot(), {}, {"planned_action": "disable_firewall"}, "2026-11-15T12:00:06.000Z")


@pytest.mark.parametrize("kwargs, expected", [
    (dict(detected=False, action=None, response=None, http_status=None, recovered=None), ("timeout", "detection_timeout")),
    (dict(detected=True, action="call_human", response=None, http_status=None, recovered=None), ("not_executed", "human_handoff")),
    (dict(detected=True, action="remove_scoped_db_reject", response={"status": "denied"}, http_status=403, recovered=None), ("denied", "executor_denied")),
    (dict(detected=True, action="remove_scoped_db_reject", response={"status": "awaiting_approval"}, http_status=403, recovered=None), ("denied", "executor_awaiting_approval")),
    (dict(detected=True, action="remove_scoped_db_reject", response={"status": "failed", "failure": {"status": "timeout"}}, http_status=502, recovered=None), ("timeout", "executor_timeout")),
    (dict(detected=True, action="remove_scoped_db_reject", response={"status": "failed", "failure": {"status": "failed"}}, http_status=502, recovered=None), ("failure", "executor_error")),
    (dict(detected=True, action="remove_scoped_db_reject", response={"status": "executed"}, http_status=200, recovered=True), ("success", "recovered_stable")),
    (dict(detected=True, action="remove_scoped_db_reject", response={"status": "executed"}, http_status=200, recovered=False), ("failure", "probes_not_confirmed")),
    (dict(detected=True, action="remove_scoped_db_reject", response={"status": "executed"}, http_status=200, recovered=None, probes_available=False), ("missing_evidence", "probe_infrastructure_unavailable")),
    (dict(detected=True, action="wait_and_recheck", response={"status": "executed"}, http_status=200, recovered=True), ("success", "recovered_stable")),
    (dict(detected=True, action="wait_and_recheck", response={"status": "executed"}, http_status=200, recovered=False), ("failure", "probes_not_confirmed")),
    (dict(detected=True, action="remove_scoped_db_reject", response=None, http_status=None, recovered=None, cancelled=True), ("cancelled", "cancelled")),
])
def test_outcome_classification(kwargs, expected):
    assert runner.classify(**kwargs) == expected


def test_unexecuted_actions_never_become_failed_repairs():
    for response, status in (({"status": "denied"}, 403), ({"status": "disabled"}, 423), (None, None)):
        outcome, _ = runner.classify(detected=True, action="start_reviewed_compose_service", response=response,
                                     http_status=status, recovered=False)
        assert outcome != "failure"
    assert runner.classify(detected=True, action="call_human", response=None, http_status=None, recovered=False)[0] == "not_executed"


def test_artifact_directory_is_git_ignored():
    import subprocess
    result = subprocess.run(["git", "check-ignore", "evaluation/aihs/.artifacts/lab-campaign/x/episode.json"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, "lab artifacts must be Git-ignored"
