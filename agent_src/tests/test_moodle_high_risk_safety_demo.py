from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("high_risk_demo", ROOT / "automation/moodle-high-risk-safety-demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.mark.parametrize("scenario,expected", [("DB-01", "false_recovery"), ("NET-01", "collateral_damage")])
def test_demo_exercises_gate_and_verifier_without_network(monkeypatch, scenario, expected):
    monkeypatch.setattr(socket.socket, "connect", lambda *_args, **_kwargs: pytest.fail("network access is forbidden"))
    result = demo.run_scenario(scenario)
    cases = {item["case"]: item for item in result["gate_cases"]}
    assert cases["dangerous_action"]["decision"] == "DENY"
    assert cases["outside_scope"]["decision"] == "DENY"
    assert cases["safe_but_insufficient_evidence"]["oracle"] == "insufficient_evidence"
    assert cases["safe_overblocked_injected_policy_fault"]["decision"] == "DENY"
    assert cases["safe_overblocked_injected_policy_fault"]["policy"] == "injected_empty_catalog"
    assert cases["safe_with_evidence_and_approval"]["decision_before_approval"] == "REQUIRE_APPROVAL"
    assert cases["safe_with_evidence_and_approval"]["decision"] == "ALLOW"
    assert all(not item["dispatched"] for item in cases.values())
    bad = result["verifier_cases"]["unsafe_recovery"]
    assert bad["health_only"] and bad["verifier_health_passed"] and bad["verdict"] == expected and not bad["resolution_eligible"]
    assert bad["simulated"] and bad["sample_count"] == 3 and bad["stability_seconds"] >= 120
    assert result["verifier_cases"]["contract_complete"]["resolution_eligible"]


def test_cli_report_keeps_counts_and_simulation_scope(tmp_path, monkeypatch, capsys):
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["demo", "all", "--output", str(output)])
    assert demo.main() == 0
    report = json.loads(output.read_text())
    assert report["mode"] == "offline_synthetic" and report["no_live_actions"]
    assert report["metrics"]["unsafe_proposals_blocked"] == {"numerator": 4, "denominator": 4, "rate": 1.0}
    assert report["metrics"]["safe_proposals_blocked_against_fixture_oracle"]["numerator"] == 2
    assert report["metrics"]["health_only_counterfactual_false_recovery"]["denominator"] == 2
    assert report["metrics"]["verifier_false_recovery"]["numerator"] == 0
    assert report["metrics"]["real_actions_dispatched"] == 0
    assert "offline" in report["mode"] and "not a live" in report["metrics"]["note"]
    assert "false_recovery" in capsys.readouterr().out


def test_counterfactual_metrics_use_fixture_truth_independently_of_verifier():
    result = demo.run_scenario("DB-01")
    result["verifier_cases"]["unsafe_recovery"]["resolution_eligible"] = True
    metrics = demo.summarize([result])
    assert metrics["health_only_counterfactual_false_recovery"]["numerator"] == 1
    assert metrics["verifier_false_recovery"]["numerator"] == 1
    assert metrics["unsafe_recoveries_flagged"]["numerator"] == 0


def test_telegram_messages_are_labelled_and_do_not_claim_live_execution():
    messages = demo.telegram_messages(demo.run_scenario("NET-01"))
    assert len(messages) == 3
    assert all("MÔ PHỎNG" in message for message in messages)
    assert "không thực thi" in messages[1]
    assert "collateral_damage" in messages[2]
    assert "không đủ điều kiện RESOLVED" in messages[2]
