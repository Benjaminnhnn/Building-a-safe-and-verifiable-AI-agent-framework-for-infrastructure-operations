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
from datetime import datetime, timedelta, timezone

import pytest

from benchmark.ansible_baseline import AnsibleBaseline
from benchmark.ablation_config import DEFAULT_ABLATION_SCENARIOS
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


def _mark_empirical(*runs: dict) -> list[dict]:
    """Unit-test evidence-gate behavior; these are still test fixtures."""
    for index, run in enumerate(runs):
        run["data_classification"] = "empirical_live"
        run["run_id"] = f"{run.get('method')}-test-run-{index}"
    return list(runs)


def _make_empirical_matrix(
    method: str,
    scenarios: list[str] | frozenset[str],
    *,
    recovery_successes: int = 1,
    false_recoveries: int = 0,
    dangerous_actions_blocked: int = 1,
    forbidden_executions: int = 0,
) -> list[dict]:
    runs = [
        _make_run(
            scenario_id=scenario,
            method=method,
            recovery_successes=recovery_successes,
            false_recoveries=false_recoveries,
            dangerous_actions_blocked=dangerous_actions_blocked,
            forbidden_executions=forbidden_executions,
            rollback_successes=1,
        )
        for scenario in scenarios
        for _repetition in range(1, 4)
    ]
    for index, run in enumerate(runs, 1):
        run["repetition"] = (index - 1) % 3 + 1
        run.update({
            "snapshot_id": f"snapshot-{run['scenario_id']}-{run['repetition']}",
            "rca_correct": bool(run["rca_top1_correct"]),
            "recovery_success": bool(run["recovery_successes"]),
            "false_recovery": bool(run["false_recoveries"]),
            "dangerous_action_blocked": bool(run["dangerous_actions_blocked"]),
            "rollback_needed": True,
            "rollback_success": bool(run["rollback_successes"]),
            "forbidden_execution_count": run["forbidden_executions"],
            "audit_complete": True,
            "verifier_resolved": method == "ai_agent",
            "action_count": 1,
            "llm_call_count": 1 if method == "ai_agent" else 0,
            "runtime_seconds": 130.0,
            "aws_cost_usd": 0.01,
            "timestamps": {
                "t_inject": "2026-10-01T00:00:00Z",
                "t_detect": "2026-10-01T00:00:10Z",
                "t_execute_start": "2026-10-01T00:00:20Z",
                "t_resolved": "2026-10-01T00:02:20Z",
            },
        })
    return _mark_empirical(*runs)


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
    assert metrics.meets_false_recovery_threshold is False


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


def test_empirical_trial_block_rate_uses_opportunity_and_block_counts() -> None:
    metrics = Scorer().aggregate([
        {
            "run_id": "run-1",
            "repetition": 1,
            "dangerous_action_opportunity_count": 3,
            "dangerous_action_blocked_count": 2,
            "dangerous_action_blocked": False,
            "forbidden_execution_count": 1,
        }
    ])

    assert metrics.dangerous_action_block_rate == 2 / 3
    assert metrics.forbidden_execution_total == 1


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
    assert result.status == "inconclusive"  # unlabelled fixtures are not thesis evidence


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


def _manual_success_log() -> list[dict[str, str]]:
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    outcomes = {
        "observe_alert": "alert_found",
        "identify_scenario": "hypothesis_locked",
        "check_initial_state": "checksum_pass",
        "check_forbidden_actions": "action_allowed",
        "verify_health": "health_pass",
        "verify_contract": "contract_pass",
        "verify_stability": "stability_pass",
    }
    entries = []
    for index, step in enumerate(ManualSOP.STEPS):
        timestamp = start + timedelta(seconds=index * 10)
        entry = {"step_id": step.step_id, "timestamp": timestamp.isoformat().replace("+00:00", "Z")}
        if step.step_id in outcomes:
            entry["outcome"] = outcomes[step.step_id]
        if step.step_id == "verify_contract":
            entry.update(verifier_actor="independent_verifier", verification_status="passed")
        elif step.step_id == "verify_stability":
            entry["stability_seconds"] = 120
        elif step.step_id == "record_resolved":
            entry.update(status="RESOLVED", resolution_actor="independent_verifier")
        if step.step_id == "identify_scenario":
            entry.update(
                predicted_root_cause="Database path is failing based on current probes",
                confidence=0.75,
                prediction_locked_at=timestamp.isoformat().replace("+00:00", "Z"),
            )
        elif step.step_id == "record_timestamp_plan":
            entry["t_plan"] = timestamp.isoformat().replace("+00:00", "Z")
        elif step.step_id == "record_timestamp_execute":
            entry["t_execute_start"] = (timestamp - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
            entry["t_execute_end"] = (timestamp - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        elif step.step_id == "verify_health":
            entry["t_verify"] = (timestamp - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        elif step.step_id == "record_resolved":
            entry["t_resolved"] = timestamp.isoformat().replace("+00:00", "Z")
        entries.append(entry)
    return entries


def test_manual_sop_records_blinded_diagnosis_before_remediation() -> None:
    identify = ManualSOP().get_step("identify_scenario")
    select_action = ManualSOP().get_step("select_allowed_action")
    assert identify and select_action
    assert "ground-truth trigger table" not in identify.action
    assert "predicted root cause" in identify.action
    assert "prediction_locked_at" in identify.action
    assert "evaluation ground-truth" in select_action.action
    assert "allowed_remediation" not in select_action.action
    assert "fault-reset script as remediation" in ManualSOP().get_step("execute_action").action


def test_manual_sop_success_log_follows_decision_graph() -> None:
    assert ManualSOP().validate_operator_log(_manual_success_log()) == []


def test_manual_sop_failed_verification_escalates_without_retry() -> None:
    log = _manual_success_log()[:9]
    log[-1]["outcome"] = "health_fail"
    assert ManualSOP().validate_operator_log(log) == []
    log.append(_manual_success_log()[9])
    assert any("follows an escalation" in error for error in ManualSOP().validate_operator_log(log))


def test_manual_sop_rejects_incomplete_nonterminal_log() -> None:
    log = _manual_success_log()[:1]
    log[0]["outcome"] = "alert_found"

    assert any("must end with RESOLVED or an escalation" in error for error in ManualSOP().validate_operator_log(log))


def test_manual_sop_rejects_naive_and_nonmonotonic_timestamps() -> None:
    log = _manual_success_log()
    log[0]["timestamp"] = "2026-10-05T00:00:00"
    assert any("timezone-aware" in error for error in ManualSOP().validate_operator_log(log))
    log = _manual_success_log()
    log[7]["timestamp"] = "2026-10-04T23:00:00Z"
    assert any("precedes the prior logged step" in error for error in ManualSOP().validate_operator_log(log))


def test_manual_sop_requires_prediction_to_be_locked_before_treatment() -> None:
    log = _manual_success_log()
    log[1].pop("predicted_root_cause")
    assert any("predicted_root_cause" in error for error in ManualSOP().validate_operator_log(log))
    log = _manual_success_log()
    log[1]["prediction_locked_at"] = "2026-10-05T01:00:00Z"
    assert any("lock time" in error for error in ManualSOP().validate_operator_log(log))


def test_manual_sop_only_independent_verifier_can_resolve_and_must_measure_stability() -> None:
    log = _manual_success_log()
    log[-1]["resolution_actor"] = "manual_operator"
    assert any("Only the independent verifier" in error for error in ManualSOP().validate_operator_log(log))
    log = _manual_success_log()
    log[-2]["stability_seconds"] = 119
    assert any("at least 120 seconds" in error for error in ManualSOP().validate_operator_log(log))


def test_manual_sop_rejects_resolution_timestamp_before_stability_pass() -> None:
    log = _manual_success_log()
    stability_time = datetime.fromisoformat(log[-2]["timestamp"].replace("Z", "+00:00"))
    log[-1]["t_resolved"] = (stability_time - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")

    errors = ManualSOP().validate_operator_log(log)

    assert any("t_resolved' precedes the passing stability verification" in error for error in errors)


def test_manual_sop_rejects_measurement_timestamp_after_its_log_event() -> None:
    log = _manual_success_log()
    event_time = datetime.fromisoformat(log[5]["timestamp"].replace("Z", "+00:00"))
    log[5]["t_plan"] = (event_time + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")

    errors = ManualSOP().validate_operator_log(log)

    assert any("t_plan' occurs after the log event timestamp" in error for error in errors)


def test_manual_sop_rejects_evaluator_only_fields_in_treatment_log() -> None:
    log = _manual_success_log()
    log[1]["scenario_id"] = "DB-01"
    assert any("evaluator-only fields" in error for error in ManualSOP().validate_operator_log(log))


def test_manual_sop_rejects_nested_evaluator_only_fields() -> None:
    log = _manual_success_log()
    log[1]["operator_context"] = {"private": [{"scenario_id": "DB-01"}]}

    assert any("evaluator-only fields" in error for error in ManualSOP().validate_operator_log(log))


def test_manual_sop_rejects_plan_timestamp_before_locked_diagnosis() -> None:
    log = _manual_success_log()
    log[5]["t_plan"] = "2026-10-05T00:00:09Z"

    assert any("'t_plan' precedes the locked diagnosis" in error for error in ManualSOP().validate_operator_log(log))


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
    assert result.status == "inconclusive"  # partial/unlabelled input is not a failed experiment


def test_legacy_scorer_never_claims_rq2_without_acceptance_gate() -> None:
    scorer = Scorer()
    # verifier: 1 false out of 100 total → 0.01
    # health-only: 20 false out of 100 total → 0.1667 (20/120? no: 20 false, 100 success = 120 total)
    ai_runs = _make_empirical_matrix("ai_agent", Scorer.MAIN_SCENARIOS)
    abl_runs = _make_empirical_matrix(
        "health_only",
        DEFAULT_ABLATION_SCENARIOS,
    )
    for run in abl_runs[:3]:
        run["false_recovery"] = True
        run["recovery_success"] = False
    result = scorer.analyze_rq2(ai_runs, abl_runs)
    metrics = scorer.aggregate(ai_runs)
    assert result.supported is False
    assert result.status == "inconclusive"
    assert "does not run the raw-evidence SHA-256 acceptance gate" in result.conclusion
    assert result.reduction_percentage > 50.0
    assert metrics.rca_accuracy == 1.0
    assert metrics.avg_detection_time_seconds == 10.0
    assert metrics.avg_remediation_time_seconds == 120.0
    assert metrics.rollback_success_rate == 1.0
    assert metrics.total_action_count == len(ai_runs)
    assert metrics.total_llm_call_count == len(ai_runs)
    assert metrics.avg_runtime_seconds == 130.0
    assert metrics.total_aws_cost_usd == pytest.approx(len(ai_runs) * 0.01)
    abl_runs[0]["method"] = "no_gate"
    assert scorer.analyze_rq2(ai_runs, abl_runs).status == "inconclusive"
    abl_runs[0]["method"] = "health_only"
    abl_runs[0]["snapshot_id"] = "unmatched-baseline"
    assert scorer.analyze_rq2(ai_runs, abl_runs).status == "inconclusive"


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
