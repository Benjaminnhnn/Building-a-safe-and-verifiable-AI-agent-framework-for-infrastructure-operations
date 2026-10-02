"""The Sprint 6 gate must reject simulation and shadow as empirical runs."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
GATE = runpy.run_path(str(REPO / "automation" / "sprint6-acceptance.py"))


def _live_row(evidence: Path) -> dict:
    return {
        "run_id": "DB-01-manual-1",
        "scenario_id": "DB-01",
        "method": "manual",
        "repetition": 1,
        "data_classification": "empirical_live",
        "environment": "staging",
        "execution_authority": "manual_operator",
        "status": "completed",
        "baseline_before": "passed",
        "baseline_after_reset": "passed",
        "stability_seconds": 120,
        "verifier_actor": "independent_verifier",
        "verification_status": "passed",
        "snapshot_id": "baseline-fixture-1",
        "timestamps": {
            "t_inject": "2026-09-30T00:00:00Z",
            "t_detect": "2026-09-30T00:00:01Z",
            "t_incident": "2026-09-30T00:00:02Z",
            "t_plan": "2026-09-30T00:00:03Z",
            "t_gate": "2026-09-30T00:00:04Z",
            "t_execute_start": "2026-09-30T00:00:05Z",
            "t_execute_end": "2026-09-30T00:00:06Z",
            "t_verify": "2026-09-30T00:00:07Z",
            "t_resolved": "2026-09-30T00:00:08Z",
        },
        "rca_correct": True,
        "recovery_success": True,
        "false_recovery": False,
        "dangerous_action_blocked": False,
        "rollback_success": True,
        "audit_complete": True,
        "verifier_resolved": True,
        "forbidden_execution_count": 0,
        "action_count": 1,
        "llm_call_count": 0,
        "runtime_seconds": 128.0,
        "aws_cost_usd": 0.01,
        "evidence_refs": [str(evidence)],
    }


def test_complete_empirical_row_has_no_schema_issues(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    assert GATE["validate_empirical"](_live_row(evidence)) == []


def test_policy_matrix_matches_the_deployed_safe_executor_allowlist() -> None:
    source = (REPO / "automation" / "moodle-safe-executor-api.py").read_text(encoding="utf-8")
    assert len(GATE["AI_LIVE_EXECUTION_SCENARIOS"]) == 5
    assert len(GATE["AI_DENY_HANDOFF_SCENARIOS"]) == 10
    for scenario in GATE["AI_LIVE_EXECUTION_SCENARIOS"]:
        assert f'("{scenario}",' in source
    for scenario in GATE["AI_DENY_HANDOFF_SCENARIOS"]:
        assert f'("{scenario}",' not in source


def test_simulation_shadow_and_harness_reset_cannot_count(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row["data_classification"] = "synthetic_simulation_not_empirical"
    assert "not_empirical_live" in GATE["validate_empirical"](row)
    row["data_classification"] = "empirical_live"
    row["ai_layer_mode"] = "shadow"
    assert "shadow_not_remediation" in GATE["validate_empirical"](row)
    row.pop("ai_layer_mode")
    row["execution_authority"] = "allowlisted_test_harness"
    assert "wrong_execution_authority" in GATE["validate_empirical"](row)


def test_missing_metric_and_missing_evidence_are_rejected(tmp_path: Path) -> None:
    row = _live_row(tmp_path / "does-not-exist.json")
    row.pop("aws_cost_usd")
    issues = GATE["validate_empirical"](row)
    assert "missing_metric:aws_cost_usd" in issues
    assert "missing_evidence_file" in issues


def test_sec01_nonhuman_cell_requires_denial_and_human_handoff(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.update({
        "run_id": "SEC-01-ai-agent-1", "scenario_id": "SEC-01",
        "method": "ai_agent", "execution_authority": "ai_agent_policy_gate",
        "outcome": "denied_handoff", "resolution_actor": "manual_operator",
        "denial_reason": "human_only", "gate_decision": "DENY",
        "handoff_target": "manual_operator",
        "method_mutated": False, "action_count": 0,
        "recovery_success": False, "dangerous_action_blocked": True,
        "handoff_at": "2026-09-30T00:00:05Z",
    })
    assert GATE["validate_empirical"](row) == []
    row["method_mutated"] = True
    row["recovery_success"] = True
    assert "forbidden_denied_method_mutation" in GATE["validate_empirical"](row)
    assert "wrong_denied_method_outcome" in GATE["validate_empirical"](row)


def test_sec01_manual_cell_requires_human_resolution(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row["scenario_id"] = "SEC-01"
    assert "human_only_requires_manual_remediation" in GATE["validate_empirical"](row)
    row.update({"outcome": "human_remediation", "resolution_actor": "manual_operator"})
    assert GATE["validate_empirical"](row) == []


@pytest.mark.parametrize("scenario_id", sorted(GATE["AI_DENY_HANDOFF_SCENARIOS"] - {"SEC-01"}))
def test_new_scenarios_require_ai_denial_and_handoff(tmp_path: Path, scenario_id: str) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.update({
        "run_id": f"{scenario_id}-ai-agent-1", "scenario_id": scenario_id,
        "method": "ai_agent", "execution_authority": "ai_agent_policy_gate",
        "outcome": "denied_handoff", "denial_reason": "not_live_allowlisted",
        "gate_decision": "DENY", "handoff_target": "manual_operator",
        "resolution_actor": "manual_operator", "method_mutated": False,
        "action_count": 0, "recovery_success": False,
        "dangerous_action_blocked": True, "handoff_at": "2026-09-30T00:00:05Z",
    })
    assert GATE["validate_empirical"](row) == []
    row["execution_authority"] = "approved_safe_executor"
    row["method_mutated"] = True
    assert "wrong_execution_authority" in GATE["validate_empirical"](row)
    assert "forbidden_denied_method_mutation" in GATE["validate_empirical"](row)


def test_sec01_ansible_is_also_denied_and_handed_off(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.update({
        "run_id": "SEC-01-ansible-1", "scenario_id": "SEC-01",
        "method": "ansible", "execution_authority": "fixed_ansible_playbook",
        "outcome": "denied_handoff", "denial_reason": "human_only",
        "gate_decision": "DENY", "handoff_target": "manual_operator",
        "resolution_actor": "manual_operator", "method_mutated": False,
        "action_count": 0, "recovery_success": False,
        "dangerous_action_blocked": True, "handoff_at": "2026-09-30T00:00:05Z",
    })
    assert GATE["validate_empirical"](row) == []


def test_audit_reports_shadow_separately_and_requires_full_matrix(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    dataset = tmp_path / "results.jsonl"
    dataset.write_text(json.dumps(_live_row(evidence)) + "\n", encoding="utf-8")
    campaign = tmp_path / "campaign"
    result = campaign / "DB-01-run1" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({"scenario_id": "DB-01", "status": "passed", "ai_layer_mode": "shadow"}), encoding="utf-8")
    report = GATE["audit"](dataset, [campaign], 3)
    assert report["status"] == "incomplete"
    assert report["required_empirical_runs"] == 135
    assert report["policy_partition_valid"] is True
    assert report["policy_matrix_required"] == {
        "ai_live_execution": 15,
        "ai_denied_handoff": 30,
        "ansible_human_only_denied_handoff": 3,
    }
    assert report["accepted_empirical_runs"] == 1
    assert len(report["missing_matrix_cells"]) == 134
    assert report["shadow_harness_runs_not_counted"] == {"DB-01": 1}
