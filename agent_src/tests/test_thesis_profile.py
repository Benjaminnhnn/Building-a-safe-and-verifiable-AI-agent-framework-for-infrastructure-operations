"""Seven-scenario thesis evaluation profile for RQ1 and RQ2."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.moodle.planner import BUSINESS_SCENARIO_REMEDIATION_DISPOSITION, SCENARIO_REMEDIATION_DISPOSITION
from core.safety.action_catalog import MOODLE_7_SCENARIO_IDS, ActionCatalog
from core.safety.safety_policy_engine import _is_forbidden_action
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS
from evaluation.benchmark import thesis_profile
from evaluation.benchmark.ansible_baseline import (
    DENIED_HANDOFF_SCENARIOS,
    FORBIDDEN_MODULES,
    PLAYBOOK_FILE,
    AnsibleBaseline,
)
from evaluation.benchmark.manual_protocol import OPERATOR_ACTION_CATALOG
from evaluation.benchmark.statistical_analysis import ACCEPTANCE, analyze
from test_statistical_analysis import _ablation_rows, _accepted_main_rows, _write_jsonl


def _thesis_main_rows(tmp_path: Path) -> list[dict]:
    return _accepted_main_rows(
        tmp_path,
        scenarios=thesis_profile.THESIS_SCENARIO_IDS,
        root_causes=thesis_profile.ROOT_CAUSE,
        ai_denied=thesis_profile.HUMAN_ONLY_SCENARIOS,
    )


def test_profile_matches_the_agent_seven_scenario_scope() -> None:
    assert thesis_profile.THESIS_SCENARIO_IDS == frozenset(MOODLE_7_SCENARIO_IDS)
    assert thesis_profile.HUMAN_ONLY_SCENARIOS == {"SEC-01"}
    assert SCENARIO_REMEDIATION_DISPOSITION["SEC-01"]["disposition"] == "human_only_escalation"
    for scenario_id in thesis_profile.AI_LIVE_EXECUTION_SCENARIOS:
        disposition = (
            SCENARIO_REMEDIATION_DISPOSITION.get(scenario_id)
            or BUSINESS_SCENARIO_REMEDIATION_DISPOSITION[scenario_id]
        )
        assert disposition["disposition"] == "s3_3_live_allowlisted"
        assert disposition["catalog_action_id"] == thesis_profile.THESIS_SCENARIOS[scenario_id].allowed_action[0]


def test_shared_scenarios_keep_the_legacy_policy_partition() -> None:
    shared = thesis_profile.THESIS_SCENARIO_IDS & ACCEPTANCE["SCENARIOS"]
    assert shared == {"CON-01", "DB-01", "RES-01", "NET-01", "SEC-01"}
    assert thesis_profile.AI_LIVE_EXECUTION_SCENARIOS & shared == ACCEPTANCE["AI_LIVE_EXECUTION_SCENARIOS"] & shared
    assert thesis_profile.HUMAN_ONLY_SCENARIOS == ACCEPTANCE["HUMAN_ONLY_SCENARIOS"] & shared
    assert thesis_profile.HUMAN_ONLY_SCENARIOS <= ACCEPTANCE["AI_DENY_HANDOFF_SCENARIOS"]


@pytest.mark.parametrize("scenario_id", sorted(thesis_profile.THESIS_SCENARIO_IDS))
def test_rq1_gate_denies_every_dangerous_opportunity_and_allows_only_the_reviewed_action(scenario_id: str) -> None:
    scenario = thesis_profile.THESIS_SCENARIOS[scenario_id]
    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    assert scenario.dangerous_actions
    for action_id, target_scope in scenario.dangerous_actions:
        assert not catalog.is_safe_execution_allowed(
            action_id, scenario_id=scenario_id, target_scope=target_scope, environment="staging", role="executor"
        ), f"{scenario_id} gate allows dangerous {action_id} on {target_scope}"
    if scenario.allowed_action is None:
        return
    action_id, target_scope = scenario.allowed_action
    assert catalog.is_safe_execution_allowed(
        action_id, scenario_id=scenario_id, target_scope=target_scope, environment="staging", role="executor"
    )
    assert not catalog.is_safe_execution_allowed(
        action_id, scenario_id=scenario_id, target_scope=target_scope, environment="production", role="executor"
    )


@pytest.mark.parametrize(
    "action_id",
    ["modify_security_group_rds_ingress", "truncate_mdl_task_adhoc", "drop_table", "attach_iam_policy", "delete_database"],
)
def test_hard_deny_patterns_match_snake_case_action_ids(action_id: str) -> None:
    assert _is_forbidden_action(action_id)


@pytest.mark.parametrize("action_id", ["dropdown_probe", "diameter_check", "truncated_log_view"])
def test_hard_deny_patterns_do_not_match_inside_words(action_id: str) -> None:
    assert not _is_forbidden_action(action_id)


def test_no_reviewed_catalog_action_hits_a_hard_deny_pattern() -> None:
    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    allowed = {
        action_id
        for scenario in thesis_profile.THESIS_SCENARIOS.values()
        if scenario.allowed_action
        for action_id in [scenario.allowed_action[0]]
    }
    assert allowed <= set(catalog._entries)
    assert not any(_is_forbidden_action(action_id) for action_id in allowed)


@pytest.mark.parametrize("scenario_id", sorted(thesis_profile.AI_LIVE_EXECUTION_SCENARIOS))
def test_rq2_verifier_contract_checks_what_health_only_cannot_see(scenario_id: str) -> None:
    scenario = thesis_profile.THESIS_SCENARIOS[scenario_id]
    contract = MOODLE_LIVE_CONTRACTS[scenario_id]
    checks = set(contract["allowed"]) | set(contract["forbidden"]) | set(contract["related"])
    assert scenario.health_only_blind_spot
    assert set(scenario.verifier_checks) <= checks


def test_rq2_human_only_scenario_cannot_be_auto_resolved() -> None:
    assert "SEC-01" not in MOODLE_LIVE_CONTRACTS


def test_seven_clusters_can_reach_significance_only_if_every_scenario_agrees() -> None:
    assert thesis_profile.minimum_cluster_sign_test_p_value() == pytest.approx(2 / 128)
    assert 3 * thesis_profile.minimum_cluster_sign_test_p_value() < 0.05
    assert thesis_profile.minimum_cluster_sign_test_p_value(6) * 3 > 0.05


def test_missing_thesis_dataset_is_incomplete(tmp_path: Path) -> None:
    audit = thesis_profile.audit_thesis_dataset(tmp_path / "absent.jsonl", 5)
    assert audit["status"] == "incomplete"
    assert audit["required_empirical_runs"] == 105
    assert audit["accepted_empirical_runs"] == 0


def test_legacy_only_scenarios_are_rejected_by_the_thesis_profile(tmp_path: Path) -> None:
    legacy = _accepted_main_rows(tmp_path)
    issues = thesis_profile.validate_thesis_row(next(row for row in legacy if row["scenario_id"] == "DB-02"))
    assert issues == ["not_thesis_scenario"]


def test_business_rows_are_scored_against_thesis_root_causes(tmp_path: Path) -> None:
    rows = _thesis_main_rows(tmp_path)
    biz = next(row for row in rows if row["scenario_id"] == "BIZ-02" and row["method"] == "ai_agent")
    assert thesis_profile.validate_thesis_row(biz) == []
    wrong = dict(biz, predicted_root_cause={**biz["predicted_root_cause"], "component": "mdl_task_adhoc"})
    assert thesis_profile.validate_thesis_row(wrong) == ["rca_score_mismatch"]


def test_complete_thesis_matrix_answers_rq1_and_rq2(tmp_path: Path) -> None:
    main_rows = _thesis_main_rows(tmp_path)
    main_path = tmp_path / "main.jsonl"
    _write_jsonl(main_path, main_rows)
    main_ai = {(row["scenario_id"], row["repetition"]): row for row in main_rows if row["method"] == "ai_agent"}
    no_gate, _ = _ablation_rows(tmp_path, "no_safety_gate", thesis_profile.ABLATION_SCENARIOS)
    health_only, _ = _ablation_rows(tmp_path, "no_verifier", thesis_profile.ABLATION_SCENARIOS)
    for row in no_gate + health_only:
        key = (row["scenario_id"], row["repetition"])
        row["snapshot_id"] = main_ai[key]["snapshot_id"]
        row["action_plan_sha256"] = main_ai[key]["action_plan_sha256"]
    no_gate_path, health_path = tmp_path / "no-gate.jsonl", tmp_path / "health-only.jsonl"
    _write_jsonl(no_gate_path, no_gate)
    _write_jsonl(health_path, health_only)

    report = analyze(main_path, 5, no_gate_path, health_path, profile="thesis7")

    assert report["profile"] == "thesis7"
    assert report["main_acceptance"]["status"] == "ready"
    assert report["main_acceptance"]["required_empirical_runs"] == 105
    assert report["status"] == "analyzed"
    assert report["rq1"]["status"] == "supported"
    assert report["rq1"]["no_gate_ablation"]["paired_blocking_comparison"]["eligible_scenario_clusters"] == 7
    assert report["rq2"]["status"] == "supported"
    assert report["rq2"]["paired_test"]["scenario_clusters"] == 7
    assert any("Seven scenario clusters" in item for item in report["limitations"])


def test_thesis_analysis_rejects_the_legacy_matrix(tmp_path: Path) -> None:
    main_path = tmp_path / "legacy.jsonl"
    _write_jsonl(main_path, _accepted_main_rows(tmp_path))
    report = analyze(main_path, 5, profile="thesis7")
    assert report["status"] == "inconclusive"
    assert report["reason"] == "main_dataset_not_acceptance_ready"


def test_unknown_profile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        analyze(tmp_path / "absent.jsonl", 5, profile="ad-hoc")


def _baseline_plays() -> list[dict]:
    import yaml

    return yaml.safe_load((Path(__file__).resolve().parents[2] / PLAYBOOK_FILE).read_text(encoding="utf-8"))


@pytest.mark.parametrize("scenario_id", sorted(thesis_profile.THESIS_SCENARIO_IDS))
def test_ansible_baseline_runs_a_fixed_play_or_hands_off(scenario_id: str) -> None:
    baseline = AnsibleBaseline()
    playbook = baseline.get_playbook(scenario_id)
    if scenario_id in DENIED_HANDOFF_SCENARIOS:
        assert playbook is None
        assert not any(scenario_id.lower().replace("-", "") in play["tags"] for play in _baseline_plays())
        return
    assert playbook is not None and playbook.playbook_file == PLAYBOOK_FILE
    assert baseline.validate_playbook(playbook) == []
    assert any(playbook.tag in play["tags"] for play in _baseline_plays())


def test_baseline_playbook_uses_no_shell_or_raw_module() -> None:
    modules = {key for play in _baseline_plays() for task in play["tasks"] for key in task if "." in key}
    assert modules and not modules & set(FORBIDDEN_MODULES)


@pytest.mark.parametrize("scenario_id", sorted(thesis_profile.THESIS_SCENARIO_IDS))
def test_manual_operator_catalog_covers_each_thesis_symptom(scenario_id: str) -> None:
    action = OPERATOR_ACTION_CATALOG[thesis_profile.THESIS_SCENARIOS[scenario_id].alert_name]
    assert action.human_only is (scenario_id in thesis_profile.HUMAN_ONLY_SCENARIOS)
    assert "mdl_" not in action.command
