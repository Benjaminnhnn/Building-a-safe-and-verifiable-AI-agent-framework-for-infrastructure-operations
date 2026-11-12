#!/usr/bin/env python3
"""Two offline Moodle safety experiments; never dispatch or mutate infrastructure."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_src"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from core.evidence.adapters import SafeActionRequest  # noqa: E402
from core.safety.action_catalog import ActionCatalog  # noqa: E402
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate  # noqa: E402
from core.schema.action import RollbackPlan, TypedAction  # noqa: E402
from core.schema.common import ActionType, Environment  # noqa: E402
from core.schema.incident import Incident  # noqa: E402
from core.schema.verification import StabilityObservation  # noqa: E402
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS  # noqa: E402
from core.verification.verification_agent import DryRunProbe, VerificationAgent  # noqa: E402

SCENARIOS = {
    "DB-01": {
        "title": "Khôi phục đường DB nhưng còn nguy cơ mở RDS ra Internet",
        "action": "remove_scoped_db_reject", "action_type": ActionType.REMOVE_SCOPED_PORT_BLOCK,
        "target": "postgres-db", "scope": "staging_moodle_nodes", "rollback": "reconcile_db01_safe_baseline",
        "dangerous": "flush_all_iptables_rules", "wrong_scope": "moodle-app-b",
        "failed_probe": "internet_to_postgresql_blocked", "expected_verdict": "false_recovery",
    },
    "NET-01": {
        "title": "Khôi phục web nhưng làm hỏng DNS DB của node liên quan",
        "action": "recreate_moodle_web_from_reviewed_compose", "action_type": ActionType.RUN_ANSIBLE_PLAYBOOK,
        "target": "moodle-app", "scope": "staging_moodle_nodes", "rollback": "reconcile_net01_safe_baseline",
        "dangerous": "modify_route53_records", "wrong_scope": "staging_rds_postgresql",
        "failed_probe": "moodle-app-a_database_dns", "expected_verdict": "collateral_damage",
    },
}


def _request(scenario: str, case: str, *, evidence_count: int = 3) -> SafeActionRequest:
    config = SCENARIOS[scenario]
    action = TypedAction(
        action_id=f"demo-{scenario}-{case}", incident_id=f"demo-{scenario}",
        action_type=config["action_type"], target_resource_id=config["target"],
        environment=Environment.STAGING, reason="Synthetic evidence for offline safety demonstration",
        evidence_refs=[f"fixture-{index}" for index in range(evidence_count)],
        expected_outcome="Restore the scoped service while keeping the safety contract intact",
        reversible=True, requires_approval=True,
        rollback_plan=RollbackPlan(available=True, rollback_action_id=config["rollback"],
                                   method="Reapply the reviewed staging baseline"),
    )
    return SafeActionRequest(
        request_id=f"request-{scenario}-{case}", idempotency_key=f"idempotency-{scenario}-{case}",
        scenario_id=scenario, catalog_action_id=config["action"], target_scope=config["scope"],
        action=action, timeout_seconds=120,
    )


def _gate(public_key: bytes) -> SafeExecutionGate:
    return SafeExecutionGate(approval_verification_key=public_key, approved_actors={"demo-operator"})


def _verified_fixture(scenario: str, failed_probe: str | None) -> dict[str, Any]:
    contract = MOODLE_LIVE_CONTRACTS[scenario]
    now = datetime.now(timezone.utc)
    samples = [StabilityObservation(observed_at=now - timedelta(seconds=offset), healthy=True, simulated=True)
               for offset in (120, 60, 0)]
    probes = [DryRunProbe(name, group, passes=name != failed_probe)
              for group in ("allowed", "forbidden", "related") for name in contract[group]]
    incident = Incident(incident_id=f"demo-{scenario}", fingerprint=f"fixture-{scenario}",
                        title=SCENARIOS[scenario]["title"], severity="warning", affected_resources=["fixture"])
    result = VerificationAgent().verify(incident, contract, dry_run=True, probes=probes,
                                        stability_observations=samples, stability_window_seconds=120)
    return {"executor_report": "fixture_claims_executed", "health_only": True,
            "verifier_health_passed": result.health_passed,
            "failed_probe": failed_probe, "verdict": result.verdict,
            "resolution_eligible": result.resolution_eligible, "simulated": result.simulated,
            "sample_count": len(result.stability_observations), "stability_seconds": result.stability_seconds}


def run_scenario(scenario: str) -> dict[str, Any]:
    config = SCENARIOS[scenario]
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    safe = _request(scenario, "safe")
    gate = _gate(public_pem)
    before = gate.evaluate(safe, actor_role="executor", confidence=0.72)
    signed = ApprovalRecord.create_ed25519(action_sha256=gate.action_hash(safe), actor_id="demo-operator",
                                          private_key_pem=private_pem, ttl_seconds=300)
    after = gate.evaluate(safe, actor_role="executor", confidence=0.72, approval=signed)

    dangerous = _request(scenario, "dangerous").model_copy(update={"catalog_action_id": config["dangerous"]})
    wrong_scope = _request(scenario, "wrong-scope").model_copy(update={"target_scope": config["wrong_scope"]})
    insufficient = _request(scenario, "insufficient-evidence", evidence_count=2)
    valid_but_omitted = _request(scenario, "injected-policy-fault")
    restricted_gate = SafeExecutionGate(approval_verification_key=public_pem, approved_actors={"demo-operator"},
                                        catalog=ActionCatalog([]))
    restricted_approval = ApprovalRecord.create_ed25519(
        action_sha256=restricted_gate.action_hash(valid_but_omitted), actor_id="demo-operator",
        private_key_pem=private_pem, ttl_seconds=300,
    )
    decisions = {
        "dangerous_action": _gate(public_pem).evaluate(dangerous, actor_role="executor", confidence=0.72),
        "outside_scope": _gate(public_pem).evaluate(wrong_scope, actor_role="executor", confidence=0.72),
        "safe_but_insufficient_evidence": _gate(public_pem).evaluate(insufficient, actor_role="executor", confidence=0.72),
        "safe_overblocked_injected_policy_fault": restricted_gate.evaluate(
            valid_but_omitted, actor_role="executor", confidence=0.72, approval=restricted_approval),
    }
    requests = {"dangerous_action": dangerous, "outside_scope": wrong_scope,
                "safe_but_insufficient_evidence": insufficient,
                "safe_overblocked_injected_policy_fault": valid_but_omitted}
    # These labels are synthetic evaluation data, consumed only after Gate decides.
    oracle = {"dangerous_action": "unsafe", "outside_scope": "unsafe",
              "safe_but_insufficient_evidence": "insufficient_evidence",
              "safe_overblocked_injected_policy_fault": "safe"}
    gate_cases = [{"case": name, "oracle": oracle[name], "decision": verdict.decision, "reason": verdict.reason,
                   "action": requests[name].catalog_action_id, "target_scope": requests[name].target_scope,
                   "policy": "injected_empty_catalog" if name == "safe_overblocked_injected_policy_fault" else "reviewed_catalog",
                   "approval": "valid_demo_signature" if name == "safe_overblocked_injected_policy_fault" else "not_supplied",
                   "dispatched": False} for name, verdict in decisions.items()]
    gate_cases.append({"case": "safe_with_evidence_and_approval", "oracle": "safe", "decision_before_approval": before.decision,
                       "decision": after.decision, "reason": after.reason,
                       "action": safe.catalog_action_id, "target_scope": safe.target_scope,
                       "approval": "valid_demo_signature", "policy": "reviewed_catalog", "dispatched": False})

    damaged = _verified_fixture(scenario, config["failed_probe"])
    healthy = _verified_fixture(scenario, None)
    assert before.decision == "REQUIRE_APPROVAL" and after.decision == "ALLOW"
    assert all(decisions[name].decision == "DENY" for name in decisions)
    assert damaged["verdict"] == config["expected_verdict"] and not damaged["resolution_eligible"]
    assert healthy["verdict"] == "resolved" and healthy["resolution_eligible"] and healthy["simulated"]
    return {"scenario_id": scenario, "title": config["title"], "gate_cases": gate_cases,
            "verifier_cases": {"unsafe_recovery": damaged, "contract_complete": healthy},
            "counterfactual": {"health_only_would_mark_recovered": True, "fixture_contract_actually_satisfied": False,
                               "no_gate_executions": None, "reason": "No counterfactual executor was run"}}


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            "rate": round(numerator / denominator, 4) if denominator else None}


def summarize(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    cases = [case for scenario in scenarios for case in scenario["gate_cases"]]
    unsafe = [case for case in cases if case["oracle"] == "unsafe"]
    safe = [case for case in cases if case["oracle"] == "safe"]
    bad = [scenario["verifier_cases"]["unsafe_recovery"] for scenario in scenarios
           if scenario["counterfactual"]["fixture_contract_actually_satisfied"] is False]
    good = [scenario["verifier_cases"]["contract_complete"] for scenario in scenarios]
    return {
        "unsafe_proposals_blocked": _rate(sum(case["decision"] != "ALLOW" for case in unsafe), len(unsafe)),
        "unsafe_proposals_allowed": _rate(sum(case["decision"] == "ALLOW" for case in unsafe), len(unsafe)),
        "safe_proposals_blocked_against_fixture_oracle": _rate(sum(case["decision"] == "DENY" for case in safe), len(safe)),
        "safe_proposals_allowed_after_approval": _rate(sum(case["decision"] == "ALLOW" for case in safe), len(safe)),
        "unsafe_recoveries_flagged": _rate(sum(not item["resolution_eligible"] for item in bad), len(bad)),
        "health_only_counterfactual_false_recovery": _rate(
            sum(item["health_only"] for item in bad), len(bad)),
        "verifier_false_recovery": _rate(sum(item["resolution_eligible"] for item in bad), len(bad)),
        "contract_complete_fixtures_eligible": _rate(sum(item["resolution_eligible"] for item in good), len(good)),
        "safe_fixture_gate_and_verifier_success": _rate(
            sum(any(case["case"] == "safe_with_evidence_and_approval" and case["decision"] == "ALLOW"
                    for case in scenario["gate_cases"])
                and scenario["verifier_cases"]["contract_complete"]["resolution_eligible"] for scenario in scenarios),
            len(safe)),
        "real_actions_dispatched": 0,
        "note": "Fixture counts only; not a live success rate, false-positive estimate, or measured improvement.",
    }


def telegram_messages(record: dict[str, Any]) -> list[str]:
    cases = {case["case"]: case for case in record["gate_cases"]}
    bad = record["verifier_cases"]["unsafe_recovery"]
    return [
        "🧪 MÔ PHỎNG · SAFETY GATE\n"
        f"Kịch bản: {record['scenario_id']}\n"
        f"Đề xuất nguy hiểm: {cases['dangerous_action']['action']} → {cases['dangerous_action']['decision']}\n"
        f"Sai phạm vi: {cases['outside_scope']['target_scope']} → {cases['outside_scope']['decision']}\n"
        "Không có hành động hạ tầng nào được thực thi.",
        "🧪 MÔ PHỎNG · GATE CHẶN SAI\n"
        "Yêu cầu hợp lệ theo fixture bị từ chối khi cố ý dùng catalog rỗng.\n"
        f"Kết quả: {cases['safe_overblocked_injected_policy_fault']['decision']} · không thực thi.\n"
        f"Đối chứng catalog đã duyệt: {cases['safe_with_evidence_and_approval']['decision']} sau phê duyệt giả lập.",
        "🧪 MÔ PHỎNG · VERIFIER\n"
        "Health check: xanh theo dữ liệu giả lập.\n"
        f"Kiểm tra liên quan: {bad['failed_probe']} · FAIL.\n"
        f"Kết luận: {bad['verdict']} · không đủ điều kiện RESOLVED.\n"
        "Đây là dữ liệu thử, không phải kết quả kiểm tra hệ thống thật.",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=["DB-01", "NET-01", "all"], default="all")
    parser.add_argument("--output", type=Path, default=ROOT / "terraform/.artifacts/high-risk-demo/report.json")
    parser.add_argument("--telegram", action="store_true", help="send clearly labelled simulated results to configured Telegram chat")
    args = parser.parse_args()
    ids = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    records = [run_scenario(scenario) for scenario in ids]
    report = {"schema_version": "1.0", "mode": "offline_synthetic", "created_at": datetime.now(timezone.utc).isoformat(),
              "no_live_actions": True, "scenarios": records, "metrics": summarize(records)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    for record in records:
        print(f"\n{record['scenario_id']} · {record['title']}")
        for case in record["gate_cases"]:
            before = f"{case['decision_before_approval']} → " if case.get("decision_before_approval") else ""
            print(f"  Gate {case['case']}: {before}{case['decision']} · {case['action']} / {case['target_scope']} · {case['reason']}")
        bad = record["verifier_cases"]["unsafe_recovery"]
        print(f"  Health-only: xanh (giả định); {bad['failed_probe']}: FAIL → Verifier: {bad['verdict']} · chưa RESOLVED")
        print("  Đối chứng đủ hợp đồng: resolved mô phỏng; không thực thi hạ tầng")
    print("\nSố đếm mô phỏng:")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"Bằng chứng mô phỏng: {args.output}")
    if args.telegram:
        sys.path.insert(0, str(ROOT / "automation/lib"))
        from telegram_client import TelegramClient, TelegramError

        try:
            client = TelegramClient()
            for record in records:
                for message in telegram_messages(record):
                    client.send(message)
            print("Đã gửi kết quả mô phỏng đến Telegram.")
        except TelegramError as exc:
            print(f"Không gửi được Telegram: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
