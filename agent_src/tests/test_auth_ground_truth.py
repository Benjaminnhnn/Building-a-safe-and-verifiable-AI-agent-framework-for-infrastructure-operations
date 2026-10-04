from __future__ import annotations

import json
from pathlib import Path

from core.ground_truth import validate_ground_truth


ROOT = Path(__file__).resolve().parents[2]
AUTH_DIR = ROOT / "evaluation" / "ground_truth" / "auth"


def test_auth_ground_truth_is_separate_and_contains_three_scenarios() -> None:
    paths = sorted(AUTH_DIR.glob("*.json"))
    assert {path.stem for path in paths} == {"AUTH-01", "AUTH-02", "AUTH-03"}
    scenarios = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    assert [scenario["scenario_id"] for scenario in scenarios] == ["AUTH-01", "AUTH-02", "AUTH-03"]
    for path, scenario in zip(paths, scenarios, strict=True):
        assert validate_ground_truth(scenario, path=path) == []
        assert scenario["allowed_remediation"] == []
        assert scenario["expected_root_cause"]
        assert scenario["rollback_plan"]["idempotent"] is True


def test_auth_ground_truth_is_not_loaded_into_existing_moodle_replay_catalog() -> None:
    original_moodle_ids = {path.stem for path in (ROOT / "evaluation" / "ground_truth" / "moodle").glob("*.json")}
    assert len(original_moodle_ids) == 15
    assert not original_moodle_ids.intersection({"AUTH-01", "AUTH-02", "AUTH-03"})

