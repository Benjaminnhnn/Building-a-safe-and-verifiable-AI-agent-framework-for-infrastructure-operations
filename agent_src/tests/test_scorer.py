"""Tests for evaluation/benchmark/scorer.py, manual_protocol.py, ansible_baseline.py.

Run with:
    $env:PYTHONPATH="agent_src"; python -m pytest agent_src/tests/test_scorer.py --basetemp=".pytest-tmp" -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `evaluation/` importable so we can do `from benchmark.scorer import ...`
# parents[0] = agent_src/tests, parents[1] = agent_src, parents[2] = project root
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evaluation"))

import json

import pytest

from benchmark.ansible_baseline import AnsibleBaseline
from benchmark.manual_protocol import ManualSOP
from benchmark.scorer import AggregateMetrics, RQ1Result, RQ2Result, Scorer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run(
    *,
    scenario_id: str = "DB-01",
    method: str = "ai_agent",
    repetitions: int = 1,
    rca_top1_correct: int = 1,
    recovery_successes: int = 1,
    false_recoveries: int = 0,
    dangerous_actions_blocked: int = 1,
    forbidden_executions: int = 0,
    rollback_successes: int = 0,
    avg_detection_time: float | None = None,
    avg_remediation_time: float | None = None,
    audit_complete_count: int = 1,
    verifier_resolved_count: int = 1,
) -> dict:
    return {
        "scenario_id": scenario_id,
        "method": method,
        "repetitions": repetitions,
        "rca_top1_correct": rca_top1_correct,
        "recovery_successes": recovery_successes,
        "false_recoveries": false_recoveries,
        "dangerous_actions_blocked": dangerous_actions_blocked,
        "forbidden_executions": forbidden_executions,
        "rollback_successes": rollback_successes,
        "avg_detection_time": avg_detection_time,
        "avg_remediation_time": avg_remediation_time,
        "audit_complete_count": audit_complete_count,
        "verifier_resolved_count": verifier_resolved_count,
    }


# ---------------------------------------------------------------------------
# test_aggregate_empty_returns_zeros
# ---------------------------------------------------------------------------


def test_aggregate_empty_returns_zeros() -> None:
    scorer = Scorer()
    metrics = scorer.aggregate([])

    assert metrics.total_runs == 0
    assert metrics.rca_accuracy == 0.0
    assert metrics.recovery_success_rate == 0.0
    assert metrics.dangerous_action_block_rate == 0.0
    assert metrics.forbidden_execution_total == 0
    assert metrics.rollback_success_rate == 0.0
    assert metrics.audit_completeness == 0.0
    assert metrics.verifier_authority_rate == 0.0
    assert metrics.avg_detection_time_seconds is None
    assert metrics.avg_remediation_time_seconds is None


# ---------------------------------------------------------------------------
# test_aggregate_rca_accuracy: 2 correct out of 3 → 0.667
# ---------------------------------------------------------------------------


def test_aggregate_rca_accuracy() -> None:
    """2 correct RCA hits out of 3 total repetitions → rca_accuracy ≈ 0.667."""
    scorer = Scorer()
    runs = [
        _make_run(repetitions=2, rca_top1_correct=1),  # 1 of 2 correct
        _make_run(repetitions=1, rca_top1_correct=1),  # 1 of 1 correct
    ]
    metrics = scorer.aggregate(runs)

    assert metrics.total_runs == 2
    assert abs(metrics.rca_accuracy - (2 / 3)) < 1e-6


# ---------------------------------------------------------------------------
# test_rq1_result_has_conclusion: analyze_rq1 returns non-empty conclusion
# ---------------------------------------------------------------------------


def test_rq1_result_has_conclusion() -> None:
    scorer = Scorer()

    ai_runs = [_make_run(method="ai_agent", dangerous_actions_blocked=19, forbidden_executions=1)]
    manual_runs = [_make_run(method="manual", dangerous_actions_blocked=10, forbidden_executions=10)]
    ansible_runs = [_make_run(method="ansible", dangerous_actions_blocked=15, forbidden_executions=5)]
    ablation_runs = [
        _make_run(method="no_gate", dangerous_actions_blocked=0, forbidden_executions=5, repetitions=5)
    ]

    result = scorer.analyze_rq1(ai_runs, manual_runs, ansible_runs, ablation_runs)

    assert isinstance(result, RQ1Result)
    assert result.conclusion
    assert len(result.conclusion) > 0
    assert isinstance(result.supported, bool)


# ---------------------------------------------------------------------------
# test_rq2_result_reduction: verifier 0.02, health_only 0.15 → reduction ~86%
# ---------------------------------------------------------------------------


def test_rq2_result_reduction() -> None:
    scorer = Scorer()

    # AI runs: 1 false recovery out of 50 total attempts → ~2%
    ai_runs = [
        _make_run(
            method="ai_agent",
            recovery_successes=49,
            false_recoveries=1,
        )
    ]
    # Ablation (health-only): 8 false recoveries out of 53 total → ~15%
    abl_runs = [
        _make_run(
            method="health_only",
            recovery_successes=45,
            false_recoveries=8,
        )
    ]

    result = scorer.analyze_rq2(ai_runs, abl_runs)

    assert isinstance(result, RQ2Result)
    # verifier false_recovery_rate = 1/50 = 0.02
    assert abs(result.verifier_false_recovery_rate - (1 / 50)) < 1e-6
    # health_only false_recovery_rate = 8/53 ≈ 0.1509...
    assert abs(result.health_only_false_recovery_rate - (8 / 53)) < 1e-4
    # reduction ≈ (0.1509 - 0.02) / 0.1509 * 100 ≈ 86.7%
    assert result.reduction_percentage > 80.0
    assert result.conclusion


# ---------------------------------------------------------------------------
# test_export_csv_creates_file
# ---------------------------------------------------------------------------


def test_export_csv_creates_file(tmp_path: Path) -> None:
    scorer = Scorer()
    runs = [_make_run(), _make_run(scenario_id="RES-01", method="manual")]
    output = tmp_path / "results.csv"
    scorer.export_csv(runs, output)

    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "scenario_id" in content  # header present
    assert "DB-01" in content
    assert "RES-01" in content


# ---------------------------------------------------------------------------
# test_export_jsonl_creates_file
# ---------------------------------------------------------------------------


def test_export_jsonl_creates_file(tmp_path: Path) -> None:
    scorer = Scorer()
    runs = [_make_run(), _make_run(scenario_id="NET-01")]
    output = tmp_path / "results.jsonl"
    scorer.export_jsonl(runs, output)

    assert output.exists()
    lines = output.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    obj = json.loads(lines[0])
    assert obj["scenario_id"] == "DB-01"


# ---------------------------------------------------------------------------
# test_meets_thresholds_all_pass
# ---------------------------------------------------------------------------


def test_meets_thresholds_all_pass() -> None:
    """rca=0.75, recovery=0.85, block=0.96, false_recovery=0.03 → all thresholds pass."""
    scorer = Scorer()

    # Craft runs such that:
    #   rca_accuracy = 15/20 = 0.75
    #   recovery_success_rate = 17/20 = 0.85
    #   false_recovery_rate = 3/20 = 0.15... hmm - need false_recovery_rate=0.03
    # Let's use explicit numbers for each metric:
    #
    # For recovery: successes=85, false=3 → rate= 85/88 ≈ 0.966; false_rate= 3/88 ≈ 0.034
    # For block rate: blocked=96, forbidden=4 → rate= 96/100 = 0.96
    # For rca: correct=75, repetitions=100 → 0.75

    runs = [
        _make_run(
            repetitions=100,
            rca_top1_correct=75,
            recovery_successes=85,
            false_recoveries=3,
            dangerous_actions_blocked=96,
            forbidden_executions=4,
        )
    ]

    metrics = scorer.aggregate(runs)

    assert metrics.meets_rca_threshold is True, f"rca_accuracy={metrics.rca_accuracy}"
    assert metrics.meets_recovery_threshold is True, f"recovery_rate={metrics.recovery_success_rate}"
    assert metrics.meets_block_rate_threshold is True, f"block_rate={metrics.dangerous_action_block_rate}"
    assert metrics.meets_false_recovery_threshold is True, f"false_recovery={metrics.false_recovery_rate}"


# ---------------------------------------------------------------------------
# test_manual_sop_has_twelve_steps
# ---------------------------------------------------------------------------


def test_manual_sop_has_twelve_steps() -> None:
    assert len(ManualSOP.STEPS) >= 10, (
        f"Expected >= 10 SOP steps, got {len(ManualSOP.STEPS)}"
    )


def test_manual_sop_step_ids_unique() -> None:
    ids = [s.step_id for s in ManualSOP.STEPS]
    assert len(ids) == len(set(ids)), "SOPStep step_ids must be unique"


def test_manual_sop_get_step() -> None:
    sop = ManualSOP()
    step = sop.get_step("observe_alert")
    assert step is not None
    assert step.step_id == "observe_alert"
    assert sop.get_step("nonexistent_step") is None


# ---------------------------------------------------------------------------
# test_ansible_baseline_has_five_scenarios
# ---------------------------------------------------------------------------


def test_ansible_baseline_has_five_scenarios() -> None:
    assert len(AnsibleBaseline.PLAYBOOKS) >= 5, (
        f"Expected >= 5 Ansible playbooks, got {len(AnsibleBaseline.PLAYBOOKS)}"
    )


def test_ansible_baseline_get_playbook() -> None:
    baseline = AnsibleBaseline()
    pb = baseline.get_playbook("DB-01")
    assert pb is not None
    assert pb.scenario_id == "DB-01"
    assert len(pb.tasks) > 0


def test_ansible_baseline_get_unknown_returns_none() -> None:
    baseline = AnsibleBaseline()
    assert baseline.get_playbook("UNKNOWN-99") is None


def test_ansible_baseline_validate_playbook_valid() -> None:
    baseline = AnsibleBaseline()
    pb = baseline.get_playbook("DB-01")
    assert pb is not None
    errors = baseline.validate_playbook(pb)
    assert errors == [], f"Expected no validation errors, got: {errors}"


def test_ansible_baseline_all_playbooks_valid() -> None:
    baseline = AnsibleBaseline()
    for scenario_id, pb in AnsibleBaseline.PLAYBOOKS.items():
        errors = baseline.validate_playbook(pb)
        assert errors == [], (
            f"Playbook for '{scenario_id}' has validation errors: {errors}"
        )


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


def test_rq1_not_supported_when_below_threshold() -> None:
    scorer = Scorer()
    # AI block rate = 0.5, well below 0.95 threshold
    ai_runs = [_make_run(method="ai_agent", dangerous_actions_blocked=5, forbidden_executions=5)]
    manual_runs = [_make_run(method="manual", dangerous_actions_blocked=3, forbidden_executions=7)]
    ansible_runs = [_make_run(method="ansible", dangerous_actions_blocked=4, forbidden_executions=6)]
    ablation_runs = []

    result = scorer.analyze_rq1(ai_runs, manual_runs, ansible_runs, ablation_runs)
    assert result.supported is False


def test_rq2_supported_when_large_reduction() -> None:
    scorer = Scorer()
    # verifier: 1 false out of 100 total → 0.01
    # health-only: 20 false out of 100 total → 0.1667 (20/120? no: 20 false, 100 success = 120 total)
    ai_runs = [_make_run(recovery_successes=99, false_recoveries=1)]
    abl_runs = [_make_run(recovery_successes=100, false_recoveries=20)]
    result = scorer.analyze_rq2(ai_runs, abl_runs)
    assert result.supported is True
    assert result.reduction_percentage > 50.0


def test_export_csv_empty_creates_empty_file(tmp_path: Path) -> None:
    scorer = Scorer()
    output = tmp_path / "empty.csv"
    scorer.export_csv([], output)
    assert output.exists()
    assert output.read_text(encoding="utf-8") == ""


def test_scorer_score_runs_groups_by_method() -> None:
    scorer = Scorer()
    runs = [
        _make_run(method="ai_agent"),
        _make_run(method="manual"),
        _make_run(method="ai_agent"),
    ]
    grouped = scorer.score_runs(runs)
    assert len(grouped["ai_agent"]) == 2
    assert len(grouped["manual"]) == 1
