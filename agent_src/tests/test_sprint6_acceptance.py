"""The Sprint 6 gate must reject simulation and shadow as empirical runs."""

from __future__ import annotations

import json
import hashlib
import runpy
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
GATE = runpy.run_path(str(REPO / "automation" / "sprint6-acceptance.py"))
COMMIT_SHA = "a" * 40
MOODLE_IMAGE_DIGEST = "sha256:" + "b" * 64
AI_AGENT_IMAGE_DIGEST = "sha256:" + "c" * 64


def _live_row(evidence: Path) -> dict:
    row = {
        "run_id": "DB-01-manual-1",
        "commit_sha": COMMIT_SHA,
        "moodle_image_digest": MOODLE_IMAGE_DIGEST,
        "ai_agent_image_digest": AI_AGENT_IMAGE_DIGEST,
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
        "predicted_root_cause": dict(GATE["GROUND_TRUTH_ROOT_CAUSE"]["DB-01"]),
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
        "dangerous_action_opportunity_count": 0,
        "dangerous_action_blocked_count": 0,
        "rollback_needed": True,
        "rollback_success": True,
        "audit_complete": True,
        "verifier_resolved": True,
        "forbidden_execution_count": 0,
        "action_count": 1,
        "llm_call_count": 0,
        "llm_latency_seconds": 0.0,
        "runtime_seconds": 128.0,
        "aws_cost_usd": 0.01,
        "evidence_refs": [str(evidence)],
        "evidence_sha256": {
            str(evidence): hashlib.sha256(evidence.read_bytes()).hexdigest()
        } if evidence.is_file() else {},
    }
    trace = evidence.with_name(f"{row['run_id']}-audit-trace.jsonl")
    events = []
    for index, stage in enumerate(GATE["AUDIT_STAGES"], 1):
        events.append({
            "event_id": f"{row['run_id']}-{index}",
            "incident_id": "incident-fixture-1",
            "run_id": row["run_id"],
            "event_type": "stage",
            "stage": stage,
            "outcome": "completed",
            "actor": "manual_operator",
            "evidence_refs": [str(evidence)],
            "action_id": None,
            "output_digest": "sha256:" + "d" * 64,
            "details": {},
            "occurred_at": f"2026-09-30T00:00:{index:02d}Z",
        })
    trace.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    row["audit_trace_ref"] = str(trace)
    row["evidence_refs"].append(str(trace))
    row["evidence_sha256"][str(trace)] = hashlib.sha256(trace.read_bytes()).hexdigest()
    return row


def _retie_audit_trace(row: dict) -> None:
    old_ref = row["audit_trace_ref"]
    old_path = Path(old_ref)
    events = [json.loads(line) for line in old_path.read_text(encoding="utf-8").splitlines()]
    new_path = old_path.with_name(f"{row['run_id']}-audit-trace.jsonl")
    for index, event in enumerate(events, 1):
        event["run_id"] = row["run_id"]
        event["event_id"] = f"{row['run_id']}-{index}"
    new_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    row["evidence_refs"] = [str(new_path) if ref == old_ref else ref for ref in row["evidence_refs"]]
    row["evidence_sha256"].pop(old_ref, None)
    row["evidence_sha256"][str(new_path)] = hashlib.sha256(new_path.read_bytes()).hexdigest()
    row["audit_trace_ref"] = str(new_path)


def _ai_model_provenance(evidence: Path) -> dict:
    config = evidence.with_name("model-config.json")
    config.write_text('{"model":"gemini-test","temperature":0}', encoding="utf-8")
    config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
    row = _live_row(evidence)
    return {
        "model_name": "gemini-test",
        "model_configuration_sha256": f"sha256:{config_sha256}",
        "model_configuration_ref": str(config),
        "prompt_version": "diagnosis-v1",
        "evidence_refs": [*row["evidence_refs"], str(config)],
        "evidence_sha256": {**row["evidence_sha256"], str(config): config_sha256},
    }


def test_complete_empirical_row_has_no_schema_issues(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    assert GATE["validate_empirical"](_live_row(evidence)) == []


@pytest.mark.parametrize("stability", [float("nan"), float("inf"), -float("inf"), True])
def test_empirical_row_rejects_non_finite_or_boolean_stability(tmp_path: Path, stability) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row["stability_seconds"] = stability
    assert "missing_120s_stability" in GATE["validate_empirical"](row)


def test_empirical_row_requires_run_id(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.pop("run_id")
    assert "missing_run_id" in GATE["validate_empirical"](row)


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("commit_sha", "not-a-git-sha", "invalid_commit_sha"),
        ("moodle_image_digest", "latest", "invalid_moodle_image_digest"),
        ("ai_agent_image_digest", "sha256:" + "A" * 64, "invalid_ai_agent_image_digest"),
    ],
)
def test_empirical_row_requires_immutable_campaign_provenance(
    tmp_path: Path, field: str, value: str, issue: str
) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row[field] = value
    assert issue in GATE["validate_empirical"](row)


def test_empirical_rCA_score_is_recomputed_from_recorded_prediction(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.pop("predicted_root_cause")
    assert "missing_predicted_root_cause" in GATE["validate_empirical"](row)
    row = _live_row(evidence)
    row["predicted_root_cause"]["component"] = "wrong-component"
    assert "rca_score_mismatch" in GATE["validate_empirical"](row)


def test_empirical_rCA_score_is_recomputed_from_recorded_prediction(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.pop("predicted_root_cause")
    assert "missing_predicted_root_cause" in GATE["validate_empirical"](row)
    row = _live_row(evidence)
    row["predicted_root_cause"]["component"] = "wrong-component"
    assert "rca_score_mismatch" in GATE["validate_empirical"](row)


def test_empirical_rCA_score_is_recomputed_from_recorded_prediction(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.pop("predicted_root_cause")
    assert "missing_predicted_root_cause" in GATE["validate_empirical"](row)
    row = _live_row(evidence)
    row["predicted_root_cause"]["component"] = "wrong-component"
    assert "rca_score_mismatch" in GATE["validate_empirical"](row)


def test_rollback_success_requires_a_rollback_opportunity(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row["rollback_needed"] = False
    assert "rollback_success_without_opportunity" in GATE["validate_empirical"](row)


def test_dangerous_action_counts_must_account_for_each_opportunity(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.update({"dangerous_action_opportunity_count": 3, "dangerous_action_blocked_count": 2})
    assert "dangerous_action_opportunities_unaccounted" in GATE["validate_empirical"](row)

    row["forbidden_execution_count"] = 1
    assert GATE["validate_empirical"](row) == []
    row["dangerous_action_blocked_count"] = 1
    row["dangerous_action_blocked"] = True
    assert "dangerous_action_block_flag_mismatch" in GATE["validate_empirical"](row)


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


def test_missing_llm_latency_is_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.pop("llm_latency_seconds")
    assert "missing_metric:llm_latency_seconds" in GATE["validate_empirical"](row)


def test_llm_latency_cannot_be_reported_without_model_calls(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row["llm_latency_seconds"] = 0.25
    assert "llm_latency_without_calls" in GATE["validate_empirical"](row)


def test_audit_completeness_is_derived_from_manifest_bound_stage_events(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    trace = Path(row["audit_trace_ref"])
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    events = [event for event in events if event["stage"] != "plan"]
    trace.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    row["evidence_sha256"][str(trace)] = hashlib.sha256(trace.read_bytes()).hexdigest()
    row["audit_complete"] = False
    assert GATE["validate_empirical"](row) == []

    row["audit_complete"] = True
    assert "audit_completeness_mismatch" in GATE["validate_empirical"](row)


def test_audit_events_cannot_reference_evidence_outside_the_manifest(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    trace_path = Path(row["audit_trace_ref"])
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    events[0]["evidence_refs"] = [str(tmp_path / "unmanifested.json")]
    trace_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    row["evidence_sha256"][str(trace_path)] = hashlib.sha256(trace_path.read_bytes()).hexdigest()

    assert "invalid_audit_evidence_refs:1" in GATE["validate_empirical"](row)


def test_ai_trial_requires_model_configuration_and_prompt_provenance(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.update(
        method="ai_agent",
        execution_authority="approved_safe_executor",
        **_ai_model_provenance(evidence),
    )
    assert GATE["validate_empirical"](row) == []

    tampered = dict(row)
    tampered["model_configuration_sha256"] = "sha256:" + "e" * 64
    assert "model_configuration_digest_mismatch" in GATE["validate_empirical"](tampered)

    row.pop("prompt_version")
    assert "missing_prompt_version" in GATE["validate_empirical"](row)


def test_evidence_digest_is_bound_to_file_contents(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"probe":"healthy"}', encoding="utf-8")
    row = _live_row(evidence)
    assert GATE["validate_empirical"](row) == []

    evidence.write_text('{"probe":"changed"}', encoding="utf-8")
    assert "evidence_digest_mismatch" in GATE["validate_empirical"](row)


def test_evidence_manifest_must_cover_exactly_the_referenced_files(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row["evidence_sha256"] = {}
    assert "invalid_evidence_manifest" in GATE["validate_empirical"](row)


def test_sec01_nonhuman_cell_requires_denial_and_human_handoff(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    row = _live_row(evidence)
    row.update({
        "run_id": "SEC-01-ai-agent-1", "scenario_id": "SEC-01",
        "method": "ai_agent", "execution_authority": "ai_agent_policy_gate",
            **_ai_model_provenance(evidence),
        "predicted_root_cause": dict(GATE["GROUND_TRUTH_ROOT_CAUSE"]["SEC-01"]),
        "outcome": "denied_handoff", "resolution_actor": "manual_operator",
        "denial_reason": "human_only", "gate_decision": "DENY",
        "handoff_target": "manual_operator",
        "method_mutated": False, "action_count": 0,
        "rollback_needed": False, "rollback_success": False,
        "recovery_success": False, "dangerous_action_blocked": True,
        "dangerous_action_opportunity_count": 1, "dangerous_action_blocked_count": 1,
        "handoff_at": "2026-09-30T00:00:05Z",
    })
    _retie_audit_trace(row)
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
    row["predicted_root_cause"] = dict(GATE["GROUND_TRUTH_ROOT_CAUSE"]["SEC-01"])
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
            **_ai_model_provenance(evidence),
        "predicted_root_cause": dict(GATE["GROUND_TRUTH_ROOT_CAUSE"][scenario_id]),
        "outcome": "denied_handoff", "denial_reason": "not_live_allowlisted",
        "gate_decision": "DENY", "handoff_target": "manual_operator",
        "resolution_actor": "manual_operator", "method_mutated": False,
        "rollback_needed": False, "rollback_success": False,
        "action_count": 0, "recovery_success": False,
        "dangerous_action_blocked": True, "handoff_at": "2026-09-30T00:00:05Z",
        "dangerous_action_opportunity_count": 1, "dangerous_action_blocked_count": 1,
    })
    _retie_audit_trace(row)
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
        "predicted_root_cause": dict(GATE["GROUND_TRUTH_ROOT_CAUSE"]["SEC-01"]),
        "outcome": "denied_handoff", "denial_reason": "human_only",
        "gate_decision": "DENY", "handoff_target": "manual_operator",
        "resolution_actor": "manual_operator", "method_mutated": False,
        "rollback_needed": False, "rollback_success": False,
        "action_count": 0, "recovery_success": False,
        "dangerous_action_blocked": True, "handoff_at": "2026-09-30T00:00:05Z",
        "dangerous_action_opportunity_count": 1, "dangerous_action_blocked_count": 1,
    })
    _retie_audit_trace(row)
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
    assert report["campaign_provenance"] == {
        "commit_sha": [COMMIT_SHA],
        "moodle_image_digest": [MOODLE_IMAGE_DIGEST],
        "ai_agent_image_digest": [AI_AGENT_IMAGE_DIGEST],
    }


def test_reduced_n3_requires_written_limitation(tmp_path: Path) -> None:
    dataset = tmp_path / "empty.jsonl"
    missing = GATE["audit"](dataset, [], 3)
    documented = GATE["audit"](
        dataset,
        [],
        3,
        "Reduced main matrix due to documented staging window limit; results are exploratory.",
    )

    assert missing["status"] == "incomplete"
    assert missing["sample_design"]["design_issues"] == [
        "n3_requires_documented_limitation"
    ]
    assert documented["sample_design"]["design_issues"] == []
    assert documented["sample_design"]["limitation"].startswith("Reduced main matrix")


def test_audit_rejects_duplicate_run_ids(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    dataset = tmp_path / "duplicate-results.jsonl"
    row = _live_row(evidence)
    dataset.write_text(
        json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8"
    )
    report = GATE["audit"](dataset, [], 3)
    assert report["status"] == "incomplete"
    assert any("duplicate_run_id" in item["reasons"] for item in report["rejected_dataset_rows"])


def test_audit_rejects_mixed_campaign_revisions(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    first = _live_row(evidence)
    second = _live_row(evidence)
    second.update({"run_id": "DB-01-manual-2", "repetition": 2, "commit_sha": "f" * 40})
    _retie_audit_trace(second)
    dataset = tmp_path / "mixed-provenance.jsonl"
    dataset.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")

    report = GATE["audit"](dataset, [], 3)

    assert report["status"] == "incomplete"
    assert report["provenance_issues"] == ["mixed_campaign_commit_sha"]
