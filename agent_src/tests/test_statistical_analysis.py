"""Statistical helpers and fail-closed evidence validation tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from evaluation.benchmark.ablation_config import DEFAULT_ABLATION_SCENARIOS
from evaluation.benchmark.statistical_analysis import (
    _main_metrics,
    _validate_ablation,
    analyze,
    ACCEPTANCE,
    exact_cluster_sign_test,
    exact_mcnemar,
    holm_adjust,
    wilson_interval,
)

COMMIT_SHA = "a" * 40
MOODLE_IMAGE_DIGEST = "sha256:" + "b" * 64
AI_AGENT_IMAGE_DIGEST = "sha256:" + "c" * 64


def _write_audit_trace(
    path: Path, run_id: str, evidence_refs: list[str], actor: str = "ai_agent"
) -> str:
    events = [
        {
            "event_id": f"{run_id}-{index}",
            "incident_id": f"incident-{run_id}",
            "run_id": run_id,
            "event_type": "stage",
            "stage": stage,
            "outcome": "completed",
            "actor": actor,
            "evidence_refs": evidence_refs,
            "action_id": None,
            "output_digest": "sha256:" + "d" * 64,
            "details": {},
            "occurred_at": f"2026-10-01T00:00:{index:02d}Z",
        }
        for index, stage in enumerate(ACCEPTANCE["AUDIT_STAGES"], 1)
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retie_audit_trace(row: dict) -> None:
    old_ref = row["audit_trace_ref"]
    old_path = Path(old_ref)
    new_path = old_path.with_name(f"{old_path.stem}-retied-{row['run_id']}.jsonl")
    refs = [ref for ref in row["evidence_refs"] if ref != old_ref]
    digest = _write_audit_trace(new_path, row["run_id"], refs)
    refs.append(str(new_path))
    row["audit_trace_ref"] = str(new_path)
    row["evidence_refs"] = refs
    row["evidence_sha256"].pop(old_ref, None)
    row["evidence_sha256"][str(new_path)] = digest


def _ablation_rows(tmp_path: Path, mode: str) -> tuple[list[dict], dict[tuple[str, int], dict]]:
    evidence = tmp_path / "raw.json"
    evidence.write_text("{}", encoding="utf-8")
    model_config = tmp_path / "model-config.json"
    model_config.write_text('{"model":"gemini-test","temperature":0}', encoding="utf-8")
    model_config_sha256 = hashlib.sha256(model_config.read_bytes()).hexdigest()
    main_ai: dict[tuple[str, int], dict] = {}
    rows: list[dict] = []
    for scenario in DEFAULT_ABLATION_SCENARIOS:
        for repetition in range(1, 6):
            key = (scenario, repetition)
            main_ai[key] = {
                "run_id": f"main-{scenario}-{repetition}",
                "commit_sha": COMMIT_SHA,
                "moodle_image_digest": MOODLE_IMAGE_DIGEST,
                "ai_agent_image_digest": AI_AGENT_IMAGE_DIGEST,
                "model_name": "gemini-test",
                "model_configuration_sha256": f"sha256:{model_config_sha256}",
                "prompt_version": "prompt-test-v1",
                "snapshot_id": f"snapshot-{scenario}-{repetition}",
                "action_plan_sha256": hashlib.sha256(f"plan-{scenario}-{repetition}".encode()).hexdigest(),
                "dangerous_action_opportunity_count": 1,
                "audit_complete": True,
            }
            row = {
                "run_id": f"{mode}-{scenario}-{repetition}",
                "commit_sha": COMMIT_SHA,
                "moodle_image_digest": MOODLE_IMAGE_DIGEST,
                "ai_agent_image_digest": AI_AGENT_IMAGE_DIGEST,
                "scenario_id": scenario,
                "repetition": repetition,
                "method": mode,
                "ablation_mode": mode,
                "data_classification": "empirical_counterfactual",
                "environment": "staging",
                "model_name": "gemini-test",
                "model_configuration_sha256": f"sha256:{model_config_sha256}",
                "model_configuration_ref": str(model_config),
                "prompt_version": "prompt-test-v1",
                "baseline_before": "passed",
                "baseline_after_reset": "passed",
                "stability_seconds": 120,
                "snapshot_id": main_ai[key]["snapshot_id"],
                "action_plan_sha256": main_ai[key]["action_plan_sha256"],
                "evidence_refs": [str(evidence), str(model_config)],
                "evidence_sha256": {
                    str(evidence): hashlib.sha256(evidence.read_bytes()).hexdigest(),
                    str(model_config): model_config_sha256,
                },
                "timestamps": {
                    "t_inject": "2026-10-01T00:00:00Z",
                    "t_detect": "2026-10-01T00:00:01Z",
                    "t_incident": "2026-10-01T00:00:02Z",
                    "t_plan": "2026-10-01T00:00:03Z",
                    "t_gate": "2026-10-01T00:00:04Z",
                    "t_execute_start": "2026-10-01T00:00:05Z",
                    "t_execute_end": "2026-10-01T00:00:06Z",
                    "t_verify": "2026-10-01T00:02:07Z",
                    "t_resolved": "2026-10-01T00:02:08Z",
                },
                "runtime_seconds": 128.0,
                "aws_cost_usd": 0.01,
                "action_count": 1,
                "llm_call_count": 1,
                "llm_latency_seconds": 1.5,
                "audit_complete": True,
            }
            if mode == "no_safety_gate":
                row.update({
                    "sandbox_intercepted": True,
                    "sandbox_intercepted_action_count": 1,
                    "live_action_count": 0,
                    "shadow_would_execute": ["candidate-action"],
                    "dangerous_would_execute_count": 1,
                })
            else:
                row.update({
                    "counterfactual_only": True,
                    "canonical_resolution_actor": "independent_verifier",
                    "health_only_would_resolve": True,
                    "contract_satisfied": False,
                })
            audit_trace = tmp_path / f"{row['run_id']}-audit.jsonl"
            refs = [str(evidence), str(model_config)]
            audit_digest = _write_audit_trace(audit_trace, row["run_id"], refs, actor=mode)
            row["audit_trace_ref"] = str(audit_trace)
            refs.append(str(audit_trace))
            row["evidence_refs"] = refs
            row["evidence_sha256"][str(audit_trace)] = audit_digest
            rows.append(row)
    return rows, main_ai


def test_wilson_interval_handles_empty_and_extreme_samples() -> None:
    assert wilson_interval(0, 0) is None
    interval = wilson_interval(0, 10)
    assert interval is not None and interval[0] == 0 and interval[1] > 0
    assert wilson_interval(10, 10)[0] < 1


def test_exact_mcnemar_is_symmetric_and_handles_no_discordance() -> None:
    left = exact_mcnemar([True, True, False, False], [False, True, True, False])
    right = exact_mcnemar([False, True, True, False], [True, True, False, False])
    assert left["left_only"] == right["right_only"]
    assert left["p_value_two_sided"] == right["p_value_two_sided"]
    assert exact_mcnemar([True, False], [True, False])["p_value_two_sided"] == 1.0


def test_exact_cluster_sign_test_uses_clusters_and_handles_ties() -> None:
    result = exact_cluster_sign_test([1.0, 0.75, 1.0], [0.0, 0.25, 1.0])
    assert result["scenario_clusters"] == 3
    assert result["informative_scenarios"] == 2
    assert result["mean_paired_block_rate_difference_ai_minus_baseline"] == 0.5
    assert result["p_value_two_sided_exact_sign_test"] == 0.5
    assert exact_cluster_sign_test([0.5, 1.0], [0.5, 1.0])["p_value_two_sided_exact_sign_test"] == 1.0


def test_holm_adjustment_preserves_order_and_bounds() -> None:
    adjusted = holm_adjust({"first": 0.01, "second": 0.03})
    assert adjusted == {"first": 0.02, "second": 0.03}


def test_rq1_holm_family_keeps_missing_no_gate_comparison(tmp_path: Path) -> None:
    main_path = tmp_path / "main.jsonl"
    _write_jsonl(main_path, _accepted_main_rows(tmp_path))

    report = analyze(main_path, 5)
    comparisons = report["rq1"]["baseline_comparisons"]
    raw = {
        method: comparisons[f"ai_agent_vs_{method}"]["dangerous_action_blocking"][
            "p_value_two_sided_exact_sign_test"
        ]
        or 1.0
        for method in ("manual", "ansible")
    }
    raw["no_gate"] = 1.0
    expected = holm_adjust(raw)

    assert report["rq1"]["status"] == "inconclusive"
    for method in ("manual", "ansible"):
        result = comparisons[f"ai_agent_vs_{method}"]["dangerous_action_blocking"]
        assert result["p_value_holm"] == expected[method]


def test_dangerous_action_block_rate_uses_explicit_multi_action_counts() -> None:
    row = {
        "dangerous_action_opportunity_count": 3,
        "dangerous_action_blocked_count": 2,
        "forbidden_execution_count": 1,
        "rca_correct": True,
        "recovery_success": False,
        "false_recovery": False,
        "dangerous_action_blocked": False,
        "rollback_needed": False,
        "rollback_success": False,
        "audit_complete": True,
        "verifier_resolved": False,
        "predicted_root_cause": ACCEPTANCE["GROUND_TRUTH_ROOT_CAUSE"]["DB-01"],
        "scenario_id": "DB-01",
        "timestamps": {},
        "action_count": 1,
        "llm_call_count": 1,
        "llm_latency_seconds": 2.5,
        "runtime_seconds": 1.0,
        "aws_cost_usd": 0.0,
    }
    metric = _main_metrics([row])["dangerous_action_block_rate"]
    assert metric["blocked_actions"] == 2
    assert metric["dangerous_action_opportunities"] == 3
    assert metric["forbidden_executions"] == 1
    assert metric["rate"] == 2 / 3
    metrics = _main_metrics([row])
    assert metrics["llm_latency_seconds_total"] == pytest.approx(2.5)
    assert metrics["llm_latency_seconds_mean_per_trial"] == pytest.approx(2.5)


def test_component_confusion_matrix_keeps_unseen_predicted_labels() -> None:
    prediction = dict(ACCEPTANCE["GROUND_TRUTH_ROOT_CAUSE"]["DB-01"])
    prediction["component"] = "unseen_component"
    row = {
        "scenario_id": "DB-01",
        "predicted_root_cause": prediction,
        "dangerous_action_opportunity_count": 0,
        "dangerous_action_blocked_count": 0,
        "forbidden_execution_count": 0,
        "rca_correct": False,
        "recovery_success": False,
        "false_recovery": False,
        "dangerous_action_blocked": False,
        "rollback_needed": False,
        "rollback_success": False,
        "audit_complete": True,
        "verifier_resolved": False,
        "timestamps": {},
        "action_count": 0,
        "llm_call_count": 0,
        "llm_latency_seconds": 0.0,
        "runtime_seconds": 1.0,
        "aws_cost_usd": 0.0,
    }

    confusion = _main_metrics([row])["rca_component_confusion_matrix"]

    assert "unseen_component" in confusion["labels"]
    actual_component = ACCEPTANCE["GROUND_TRUTH_ROOT_CAUSE"]["DB-01"]["component"]
    assert confusion["counts"][actual_component]["unseen_component"] == 1


def test_no_gate_ablation_requires_shadow_intercept_and_matching_matrix(tmp_path: Path) -> None:
    rows, main_ai = _ablation_rows(tmp_path, "no_safety_gate")
    assert _validate_ablation(rows, "no_safety_gate", 5, main_ai) == []
    rows[0]["live_action_count"] = 1
    assert any("unsafe_or_unmatched_shadow_record" in issue for issue in _validate_ablation(rows, "no_safety_gate", 5, main_ai))
    rows[0]["live_action_count"] = False
    assert any("unsafe_or_unmatched_shadow_record" in issue for issue in _validate_ablation(rows, "no_safety_gate", 5, main_ai))


def test_ablation_must_use_the_exact_main_campaign_provenance(tmp_path: Path) -> None:
    rows, main_ai = _ablation_rows(tmp_path, "no_safety_gate")
    rows[0]["ai_agent_image_digest"] = "sha256:" + "d" * 64
    issues = _validate_ablation(rows, "no_safety_gate", 5, main_ai)
    assert any("ablation_campaign_provenance_mismatch:1" in issue for issue in issues)


def test_ablation_n3_cannot_satisfy_planned_50_run_minimum(tmp_path: Path) -> None:
    rows, main_ai = _ablation_rows(tmp_path, "no_safety_gate")
    rows = [row for row in rows if row["repetition"] <= 3]
    issues = _validate_ablation(rows, "no_safety_gate", 3, {
        key: value for key, value in main_ai.items() if key[1] <= 3
    })

    assert "ablation_requires_5_repetitions_50_runs" in issues


def test_health_only_ablation_is_counterfactual_and_verifier_retains_authority(tmp_path: Path) -> None:
    rows, main_ai = _ablation_rows(tmp_path, "no_verifier")
    assert _validate_ablation(rows, "no_verifier", 5, main_ai) == []
    rows[0]["canonical_resolution_actor"] = "health_check"
    assert any("invalid_health_only_counterfactual" in issue for issue in _validate_ablation(rows, "no_verifier", 5, main_ai))


def test_ablations_require_latency_and_match_main_ai_model_provenance(tmp_path: Path) -> None:
    rows, main_ai = _ablation_rows(tmp_path, "no_safety_gate")
    assert _validate_ablation(rows, "no_safety_gate", 5, main_ai) == []

    rows[0]["llm_call_count"] = 0
    assert any(
        "invalid_metric:llm_latency_without_calls:1" in issue
        for issue in _validate_ablation(rows, "no_safety_gate", 5, main_ai)
    )
    rows[0]["llm_call_count"] = 1

    rows[0].pop("llm_latency_seconds")
    assert any(
        "invalid_metric:llm_latency_seconds:1" in issue
        for issue in _validate_ablation(rows, "no_safety_gate", 5, main_ai)
    )
    rows[0]["llm_latency_seconds"] = 1.5

    rows[0]["prompt_version"] = "different-prompt"
    assert any(
        "ablation_model_provenance_mismatch:1" in issue
        for issue in _validate_ablation(rows, "no_safety_gate", 5, main_ai)
    )

    rows[0]["prompt_version"] = "prompt-test-v1"
    rows[0]["model_configuration_sha256"] = "sha256:" + "e" * 64
    assert any(
        "invalid_ai_model_provenance:1:model_configuration_digest_mismatch" in issue
        for issue in _validate_ablation(rows, "no_safety_gate", 5, main_ai)
    )


@pytest.mark.parametrize("stability", [float("nan"), float("inf"), -float("inf"), True])
def test_ablation_rejects_non_finite_or_boolean_stability(tmp_path: Path, stability) -> None:
    rows, main_ai = _ablation_rows(tmp_path, "no_safety_gate")
    rows[0]["stability_seconds"] = stability
    assert any(
        "missing_120s_stability" in issue
        for issue in _validate_ablation(rows, "no_safety_gate", 5, main_ai)
    )


def test_missing_empirical_dataset_stays_inconclusive(tmp_path: Path) -> None:
    report = analyze(tmp_path / "absent.jsonl", 3)
    assert report["status"] == "inconclusive"
    assert report["reason"] == "main_dataset_not_acceptance_ready"
    assert report["rq1"]["status"] == "inconclusive"
    assert report["rq2"]["status"] == "inconclusive"


def _accepted_main_rows(tmp_path: Path) -> list[dict]:
    evidence = tmp_path / "trace.json"
    evidence.write_text("{}", encoding="utf-8")
    model_config = tmp_path / "model-config.json"
    model_config.write_text('{"model":"gemini-test","temperature":0}', encoding="utf-8")
    model_config_sha256 = hashlib.sha256(model_config.read_bytes()).hexdigest()
    gate = ACCEPTANCE
    rows: list[dict] = []
    time_values = [f"2026-10-01T00:00:{second:02d}Z" for second in range(10)]
    for scenario in sorted(gate["SCENARIOS"]):
        for method in ("manual", "ansible", "ai_agent"):
            for repetition in range(1, 6):
                denied = (
                    method == "ai_agent" and scenario in gate["AI_DENY_HANDOFF_SCENARIOS"]
                ) or (method == "ansible" and scenario in gate["HUMAN_ONLY_SCENARIOS"])
                authority = gate["AUTHORITIES"][method]
                if method == "ai_agent" and scenario in gate["AI_DENY_HANDOFF_SCENARIOS"]:
                    authority = "ai_agent_policy_gate"
                row = {
                    "run_id": f"main-{method}-{scenario}-{repetition}",
                    "commit_sha": COMMIT_SHA,
                    "moodle_image_digest": MOODLE_IMAGE_DIGEST,
                    "ai_agent_image_digest": AI_AGENT_IMAGE_DIGEST,
                    "scenario_id": scenario,
                    "method": method,
                    "repetition": repetition,
                    "data_classification": "empirical_live",
                    "environment": "staging",
                    "model_name": "gemini-test" if method == "ai_agent" else None,
                    "model_configuration_sha256": f"sha256:{model_config_sha256}" if method == "ai_agent" else None,
                    "model_configuration_ref": str(model_config) if method == "ai_agent" else None,
                    "prompt_version": "prompt-test-v1" if method == "ai_agent" else None,
                    "execution_authority": authority,
                    "status": "completed",
                    "baseline_before": "passed",
                    "baseline_after_reset": "passed",
                    "stability_seconds": 120,
                    "verifier_actor": "independent_verifier",
                    "verification_status": "passed",
                    "snapshot_id": f"snapshot-{scenario}-{repetition}",
                    "action_plan_sha256": hashlib.sha256(f"plan-{scenario}-{repetition}".encode()).hexdigest(),
                    "predicted_root_cause": dict(ACCEPTANCE["GROUND_TRUTH_ROOT_CAUSE"][scenario]),
                    "timestamps": dict(zip(("t_inject", "t_detect", "t_incident", "t_plan", "t_gate", "t_execute_start", "t_execute_end", "t_verify", "t_resolved"), time_values[0:9])),
                    "rca_correct": True,
                    "recovery_success": not denied,
                    "false_recovery": False,
                    "dangerous_action_blocked": method == "ai_agent" or denied,
                    "dangerous_action_opportunity_count": int(method == "ai_agent" or denied) + int(method in ("manual", "ansible") and not denied),
                    "dangerous_action_blocked_count": int(method == "ai_agent" or denied),
                    "rollback_needed": not denied,
                    "rollback_success": not denied,
                    "audit_complete": True,
                    "verifier_resolved": method == "ai_agent" and not denied,
                    "forbidden_execution_count": int(method in ("manual", "ansible") and not denied),
                    "action_count": 0 if denied else 1,
                    "llm_call_count": int(method == "ai_agent"),
                    "llm_latency_seconds": 1.5 if method == "ai_agent" else 0.0,
                    "runtime_seconds": 130.0,
                    "aws_cost_usd": 0.01,
                    "evidence_refs": (
                        [str(evidence), str(model_config)]
                        if method == "ai_agent"
                        else [str(evidence)]
                    ),
                    "evidence_sha256": {
                        str(evidence): hashlib.sha256(evidence.read_bytes()).hexdigest(),
                        **({str(model_config): model_config_sha256} if method == "ai_agent" else {}),
                    },
                }
                if denied:
                    row.update({
                        "outcome": "denied_handoff",
                        "resolution_actor": "manual_operator",
                        "denial_reason": "human_only" if scenario in gate["HUMAN_ONLY_SCENARIOS"] else "not_live_allowlisted",
                        "gate_decision": "DENY",
                        "handoff_target": "manual_operator",
                        "method_mutated": False,
                        "handoff_at": "2026-10-01T00:00:05Z",
                    })
                elif scenario in gate["HUMAN_ONLY_SCENARIOS"]:
                    row.update({"outcome": "human_remediation", "resolution_actor": "manual_operator"})
                audit_trace = tmp_path / f"{row['run_id']}-audit.jsonl"
                audit_digest = _write_audit_trace(
                    audit_trace, row["run_id"], row["evidence_refs"], actor=method
                )
                row["audit_trace_ref"] = str(audit_trace)
                row["evidence_refs"].append(str(audit_trace))
                row["evidence_sha256"][str(audit_trace)] = audit_digest
                rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_complete_empirical_matrix_produces_analysis_only_with_both_ablations(tmp_path: Path) -> None:
    main_rows = _accepted_main_rows(tmp_path)
    main_path = tmp_path / "main.jsonl"
    _write_jsonl(main_path, main_rows)
    main_ai = {
        (row["scenario_id"], row["repetition"]): row
        for row in main_rows
        if row["method"] == "ai_agent"
    }

    no_gate, _ = _ablation_rows(tmp_path, "no_safety_gate")
    for row in no_gate:
        row["snapshot_id"] = main_ai[(row["scenario_id"], row["repetition"])]["snapshot_id"]
        row["action_plan_sha256"] = main_ai[(row["scenario_id"], row["repetition"])]["action_plan_sha256"]
    no_gate_path = tmp_path / "no-gate.jsonl"
    _write_jsonl(no_gate_path, no_gate)

    health_only, _ = _ablation_rows(tmp_path, "no_verifier")
    for row in health_only:
        row["snapshot_id"] = main_ai[(row["scenario_id"], row["repetition"])]["snapshot_id"]
    health_path = tmp_path / "health-only.jsonl"
    _write_jsonl(health_path, health_only)

    report = analyze(main_path, 5, no_gate_path, health_path)
    assert report["main_acceptance"]["status"] == "ready"
    assert report["status"] == "analyzed"
    assert report["rq1"]["status"] == "supported"
    assert report["rq1"]["conditions"]["significantly_lower_forbidden_execution_than_no_gate"] is True
    assert report["rq1"]["no_gate_ablation"]["paired_blocking_comparison"]["eligible_scenario_clusters"] == 10
    assert report["rq1"]["no_gate_ablation"]["paired_blocking_comparison"]["p_value_two_sided_exact_sign_test"] == pytest.approx(0.001953125)
    assert report["rq2"]["status"] == "supported"
    assert report["rq2"]["paired_test"]["scenario_clusters"] == 10
    assert report["rq2"]["paired_test"]["inferential_unit"] == "scenario; repetitions aggregated within scenario"
    assert report["rq2"]["paired_test"]["p_value_two_sided_exact_sign_test"] < 0.05
    assert report["rq2"]["conditions"]["scenario_level_reduction_is_significant"] is True
    assert report["provenance"]["commit_sha"] == COMMIT_SHA
    assert report["provenance"]["ai_model"]["model_name"] == ["gemini-test"]
    assert report["provenance"]["ai_model"]["model_configuration_refs"] == [
        str(tmp_path / "model-config.json")
    ]
    assert report["provenance"]["main_run_ids_by_method"]["ai_agent"] and len(report["provenance"]["main_run_ids_by_method"]["ai_agent"]) == 75
    assert len(report["provenance"]["ablation_run_ids"]["no_safety_gate"]) == 50
    assert len(report["provenance"]["ablation_run_ids"]["no_verifier"]) == 50
    assert report["methods"]["ai_agent"]["rca_top1"]["ci95_wilson"] is not None
    manual_comparison = report["rq1"]["baseline_comparisons"]["ai_agent_vs_manual"]
    assert manual_comparison["dangerous_action_blocking"]["eligible_scenarios_with_opportunities_in_both_methods"] == 15
    assert manual_comparison["dangerous_action_blocking"]["direction_ai_more_blocked"] is True
    assert "forbidden_execution_by_trial_descriptive" in manual_comparison


def test_cross_ablation_duplicate_run_id_clears_rq_support_status(tmp_path: Path) -> None:
    main_rows = _accepted_main_rows(tmp_path)
    main_path = tmp_path / "main.jsonl"
    _write_jsonl(main_path, main_rows)
    main_ai = {
        (row["scenario_id"], row["repetition"]): row
        for row in main_rows
        if row["method"] == "ai_agent"
    }
    no_gate, _ = _ablation_rows(tmp_path, "no_safety_gate")
    health_only, _ = _ablation_rows(tmp_path, "no_verifier")
    for row in no_gate:
        key = (row["scenario_id"], row["repetition"])
        row["snapshot_id"] = main_ai[key]["snapshot_id"]
        row["action_plan_sha256"] = main_ai[key]["action_plan_sha256"]
    for row in health_only:
        key = (row["scenario_id"], row["repetition"])
        row["snapshot_id"] = main_ai[key]["snapshot_id"]
    health_only[0]["run_id"] = no_gate[0]["run_id"]
    _retie_audit_trace(health_only[0])
    no_gate_path = tmp_path / "no-gate.jsonl"
    health_path = tmp_path / "health-only.jsonl"
    _write_jsonl(no_gate_path, no_gate)
    _write_jsonl(health_path, health_only)

    report = analyze(main_path, 5, no_gate_path, health_path)

    assert report["status"] == "inconclusive"
    assert report["reason"] == "duplicate_run_ids_across_datasets"
    assert report["duplicate_run_ids"] == [no_gate[0]["run_id"]]
    assert report["rq1"]["status"] == "inconclusive"
    assert report["rq1"]["status_before_duplicate_check"] == "supported"
    assert report["rq2"]["status"] == "inconclusive"
    assert report["rq2"]["status_before_duplicate_check"] == "supported"


def test_rq2_does_not_support_thresholds_without_scenario_level_significance(tmp_path: Path) -> None:
    main_rows = _accepted_main_rows(tmp_path)
    main_path = tmp_path / "main.jsonl"
    _write_jsonl(main_path, main_rows)
    main_ai = {
        (row["scenario_id"], row["repetition"]): row
        for row in main_rows
        if row["method"] == "ai_agent"
    }
    health_only, _ = _ablation_rows(tmp_path, "no_verifier")
    for row in health_only:
        key = (row["scenario_id"], row["repetition"])
        row["snapshot_id"] = main_ai[key]["snapshot_id"]
        if row["scenario_id"] in DEFAULT_ABLATION_SCENARIOS[:5]:
            row["contract_satisfied"] = True
    health_path = tmp_path / "health-only.jsonl"
    _write_jsonl(health_path, health_only)

    report = analyze(main_path, 5, health_only_path=health_path)

    rq2 = report["rq2"]
    assert rq2["relative_reduction"] == 1.0
    assert rq2["paired_test"]["p_value_two_sided_exact_sign_test"] == pytest.approx(0.0625)
    assert rq2["conditions"]["false_recovery_reduced_by_at_least_50_percent"] is True
    assert rq2["conditions"]["scenario_level_reduction_is_significant"] is False
    assert rq2["status"] == "not_supported"


def test_rq1_stays_unresolved_without_shared_dangerous_action_opportunities(tmp_path: Path) -> None:
    rows = _accepted_main_rows(tmp_path)
    for row in rows:
        if row["method"] == "manual":
            row.update({
                "dangerous_action_blocked": False,
                "dangerous_action_opportunity_count": 0,
                "dangerous_action_blocked_count": 0,
                "forbidden_execution_count": 0,
            })
    main_path = tmp_path / "main-no-manual-opportunities.jsonl"
    _write_jsonl(main_path, rows)
    report = analyze(main_path, 5)
    comparison = report["rq1"]["baseline_comparisons"]["ai_agent_vs_manual"]["dangerous_action_blocking"]
    assert report["main_acceptance"]["status"] == "ready"
    assert comparison["eligible_scenarios_with_opportunities_in_both_methods"] == 0
    assert comparison["p_value_two_sided_exact_sign_test"] is None
    assert comparison["p_value_holm"] is None
    assert report["rq1"]["conditions"]["significantly_higher_blocking_than_manual"] is False
