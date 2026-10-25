"""The thesis campaign runner emits rows the acceptance gate and analyzer accept.

Runs are simulated (fake clock and operations). Simulated rows must be
rejected as evidence for that reason only; every other validator must pass,
so a live campaign produces analyzable data without format surprises.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from evaluation.benchmark import thesis_profile
from evaluation.benchmark.statistical_analysis import _validate_ablation, analyze

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("thesis_campaign_trial", REPO / "automation" / "thesis-campaign-trial.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
SCENARIOS = sorted(thesis_profile.THESIS_SCENARIO_IDS)


def _run(dataset: Path, scenario: str, method: str, repetition: int = 1) -> None:
    assert runner.main([
        "--scenario", scenario, "--method", method, "--repetition", str(repetition),
        "--campaign-id", "camp-test", "--dataset-dir", str(dataset), "--simulate",
    ]) == 0


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def campaign(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dataset = tmp_path_factory.mktemp("campaign")
    for scenario in SCENARIOS:
        for method in runner.METHODS:
            for repetition in range(1, 6):
                _run(dataset, scenario, method, repetition)
    return dataset


def test_simulated_main_rows_fail_only_because_they_are_simulated(campaign: Path) -> None:
    rows = _rows(campaign / "main.jsonl")
    assert len(rows) == len(SCENARIOS) * 3 * 5
    for row in rows:
        assert thesis_profile.validate_thesis_row(row) == ["not_empirical_live"], row["run_id"]


def test_simulated_campaign_is_never_acceptance_ready(campaign: Path) -> None:
    audit = thesis_profile.audit_thesis_dataset(campaign / "main.jsonl", 5)
    assert audit["status"] == "incomplete"
    assert audit["accepted_empirical_runs"] == 0


def test_human_only_rows_follow_the_denied_handoff_protocol(campaign: Path) -> None:
    rows = {(row["method"], row["repetition"]): row for row in _rows(campaign / "main.jsonl") if row["scenario_id"] == "SEC-01"}
    for method in ("ai_agent", "ansible"):
        row = rows[(method, 1)]
        assert row["outcome"] == "denied_handoff" and row["method_mutated"] is False and row["action_count"] == 0
        assert row["forbidden_execution_count"] == 0 and row["dangerous_action_blocked"] is True
    assert rows[("ai_agent", 1)]["execution_authority"] == "ai_agent_policy_gate"
    assert rows[("manual", 1)]["outcome"] == "human_remediation"
    assert rows[("manual", 1)]["resolution_actor"] == "manual_operator"


def test_ai_rows_carry_deterministic_model_provenance_without_llm_calls(campaign: Path) -> None:
    for row in _rows(campaign / "main.jsonl"):
        if row["method"] == "ai_agent":
            assert row["model_name"] == runner.MODEL_NAME
            assert row["llm_call_count"] == 0 and row["llm_latency_seconds"] == 0.0
            assert row["model_configuration_ref"] in row["evidence_refs"]


@pytest.mark.parametrize(("filename", "mode"), [("no-gate.jsonl", "no_safety_gate"), ("health-only.jsonl", "no_verifier")])
def test_ablation_rows_pair_with_main_ai_rows(campaign: Path, filename: str, mode: str) -> None:
    main = _rows(campaign / "main.jsonl")
    ai_by_key = {(row["scenario_id"], row["repetition"]): row for row in main if row["method"] == "ai_agent"}
    issues = _validate_ablation(_rows(campaign / filename), mode, 5, ai_by_key, {row["run_id"] for row in main}, thesis_profile.ABLATION_SCENARIOS)
    assert issues and all(issue.startswith("not_staging_counterfactual:") for issue in issues)


def test_pipeline_output_is_analyzable_once_labelled_live(campaign: Path, tmp_path: Path) -> None:
    # Relabelling happens only inside this test to prove the row format end to
    # end; nothing in the runner can produce an empirical label in simulation.
    for name, label in (("main.jsonl", "empirical_live"), ("no-gate.jsonl", "empirical_counterfactual"), ("health-only.jsonl", "empirical_counterfactual")):
        rows = [dict(row, data_classification=label) for row in _rows(campaign / name)]
        (tmp_path / name).write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = analyze(tmp_path / "main.jsonl", 5, tmp_path / "no-gate.jsonl", tmp_path / "health-only.jsonl", profile="thesis7")
    assert report["main_acceptance"]["status"] == "ready", report["main_acceptance"]["rejected_dataset_rows"][:2]
    assert report["status"] == "analyzed"
    assert report["rq1"]["no_gate_ablation"]["status"] == "validated_counterfactual"
    assert report["rq2"]["status"] in {"supported", "not_supported"}


def test_live_mode_requires_frozen_provenance_and_cost(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        runner.parse_args(["--scenario", "BIZ-01", "--method", "ai_agent", "--repetition", "1", "--campaign-id", "c", "--dataset-dir", str(tmp_path)])
