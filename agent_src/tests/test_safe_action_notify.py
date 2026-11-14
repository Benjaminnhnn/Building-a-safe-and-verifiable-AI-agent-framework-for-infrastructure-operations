from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

import core.main as main
from core.moodle import safe_action_notify as notify_mod

SIGNATURE = "ed25519:SECRET-SIGNATURE-MUST-NOT-LEAK"
TOKEN = "t" * 64

PREPARED = {
    "status": "approval_required",
    "incident_id": "inc-1",
    "candidate_id": "ev-cand",
    "request": {
        "request_id": "req-1",
        "idempotency_key": "idem-1",
        "scenario_id": "CON-01",
        "catalog_action_id": "start_reviewed_compose_service",
        "target_scope": "moodle-app-b",
        "action": {"target_resource_id": "moodle-app-b"},
    },
    "diagnosis_confidence": 0.65,
    "gate_action_sha256": "a" * 64,
    "executor_action_sha256": "b" * 64,
    "execution_permitted": False,
    "gate_preview": {
        "decision": "REQUIRE_APPROVAL",
        "reason": "valid, action-bound approval is required",
        "checks": [f"check {n}" for n in range(8)],
        "approval_reasons": ["confidence 0.65 < 0.80"],
    },
}
EXECUTED = {
    "status": "awaiting_verification",
    "mutated": True,
    "execution_permitted": True,
    "gate": {"decision": "ALLOW", "reason": "passed", "action_sha256": "a" * 64},
    "result": {
        "status": "executed",
        "scenario_id": "CON-01",
        "catalog_action_id": "start_reviewed_compose_service",
        "target_scope": "moodle-app-b",
        "request_id": "req-1",
        "completed_nodes": ["b"],
        "failure": None,
    },
}
PAYLOAD = {
    "incident_id": "inc-1",
    "scenario_id": "CON-01",
    "verification": {"stability_seconds": 120, "verdict": "resolved"},
    "probe_samples": [{"checks": {"a": True, "b": True}}] * 4 + [{"checks": {"a": True, "b": False}}],
}


def test_prepare_message_summarizes_gate_and_waits_for_operator() -> None:
    text = notify_mod.format_prepare("ev-cand", PREPARED)
    assert "④ SAFETY GATE" in text and "ĐẠT 8/8" in text
    assert "chờ Operator" in text and "chưa thực thi" in text
    assert all(f"{index}. ✓" in text for index in range(1, 9))
    assert "a" * 64 not in text  # only a hash prefix is shown


def test_prepare_message_does_not_claim_incomplete_checks_passed() -> None:
    incomplete = {**PREPARED, "gate_preview": {"decision": "REQUIRE_APPROVAL", "checks": ["one"]}}
    text = notify_mod.format_prepare("ev-cand", incomplete)
    assert "KHÔNG XÁC ĐỊNH" in text and "Không mở phê duyệt" in text


def test_prepare_message_reports_a_gate_denial() -> None:
    denied = {**PREPARED, "gate_preview": {"decision": "DENY", "reason": "kill switch is enabled"}}
    text = notify_mod.format_prepare("ev-cand", denied)
    assert "CHẶN · kill switch is enabled" in text and "ĐẠT" not in text
    assert "start_reviewed_compose_service" in text and "moodle-app-b" in text
    assert "inc-1" in text and "ev-cand" in text
    assert "không mở phê duyệt" in text and "bằng chứng mới" in text
    assert "Gate kiểm tra lại" in text and "không nới chính sách" in text


def test_execute_message_reads_the_executor_result_not_the_workflow_status() -> None:
    # The workflow reports awaiting_verification; the executor result says executed.
    assert "⑤ EXECUTOR · CON-01 · đã thực thi" in notify_mod.format_execute("ev-cand", EXECUTED)
    blocked = {
        "status": "blocked", "mutated": False,
        "gate": {"decision": "DENY", "reason": "approval expired"},
        "result": {"status": "rejected", "mutated": False},
    }
    text = notify_mod.format_execute("ev-cand", blocked)
    assert "KHÔNG THỰC THI" in text and "approval expired" in text


def test_verification_message_counts_passing_samples() -> None:
    text = notify_mod.format_verification("req-1", PAYLOAD, {"status": "resolved", "event_hash": "c" * 64})
    assert "⑥ INDEPENDENT VERIFIER: RESOLVED" in text
    assert "4/5" in text and "80%" in text and "RPO: không áp dụng" in text


def test_alert_report_for_candidate_is_concise_and_actionable() -> None:
    report = {
        "incident_id": "inc-1",
        "status": "remediation_candidate",
        "diagnosis": {"hypothesis": "web container stopped", "cause_code": "web_container_not_running",
                      "confidence": 0.65, "affected_nodes": ["moodle-app-b"], "evidence_refs": ["a", "b"]},
        "remediation_disposition": "{'disposition': 'candidate_proposed', 'scenario_id': 'CON-01'}",
        "remediation_candidate": {
            "catalog_action_id": "start_reviewed_compose_service", "target_scope": "moodle-app-b",
            "action": {"target_resource_id": "moodle-app-b", "evidence_refs": ["e"] * 8,
                       "rollback_plan": {"rollback_action_id": "reconcile_con01_safe_baseline"}},
        },
    }
    alert = {"labels": {"alertname": "MoodleWebContainerMissing", "instance": "moodle-app-b"}}
    text = notify_mod.format_alert_report(alert, report)
    assert "MOODLE CON-01" in text and "Khởi động lại Moodle web" in text
    assert "Safety Gate kiểm tra" in text and len(text.splitlines()) <= 6


def test_alert_report_for_human_only_has_no_proposal_to_execute() -> None:
    report = {
        "incident_id": "inc-2",
        "status": "investigation_hypothesis",
        "diagnosis": {"hypothesis": "RDS port exposed", "cause_code": "database_port_exposure", "confidence": 0.65},
        "remediation_disposition": {"disposition": "human_only_escalation", "scenario_id": "SEC-01",
                                    "catalog_action_id": "remove_tagged_database_ingress_rule",
                                    "target_scope": "staging_database_security_group"},
        "remediation_candidate": None,
    }
    text = notify_mod.format_alert_report({"labels": {"alertname": "MoodleDatabasePortExposure"}}, report)
    assert "Chỉ người xử lý" in text and "rule ingress lạ" in text
    assert "Không mở luồng approve" in text


def test_notify_swallows_sender_failures() -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("telegram down")

    assert notify_mod.notify("x", sender=broken) is False


def _token_env(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="ascii")
    key_file = tmp_path / "gate.pub"
    key_file.write_text("key", encoding="ascii")
    monkeypatch.setenv("AIOPS_GATE_APPROVAL_PUBLIC_KEY_FILE", str(key_file))
    monkeypatch.setenv("AIOPS_SAFE_ACTION_API_TOKEN_FILE", str(token_file))


def test_prepare_endpoint_sends_step_four(tmp_path, monkeypatch) -> None:
    _token_env(tmp_path, monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(main, "prepare_moodle_live_handoff", lambda **_kw: PREPARED)
    monkeypatch.setattr(notify_mod, "notify", lambda message: sent.append(message) or True)

    response = TestClient(main.app).post(
        "/api/moodle/remediation/ev-cand/prepare", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    assert len(sent) == 1 and "④ SAFETY GATE" in sent[0]


def test_prepare_endpoint_operator_bridge_sends_only_its_own_message(tmp_path, monkeypatch) -> None:
    _token_env(tmp_path, monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(main, "prepare_moodle_live_handoff", lambda **_kw: PREPARED)
    monkeypatch.setattr(notify_mod, "notify", lambda message: sent.append(message) or True)
    response = TestClient(main.app).post(
        "/api/moodle/remediation/ev-cand/prepare?notify=false", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200 and sent == []


def test_prepare_endpoint_still_notifies_denied_gate(tmp_path, monkeypatch) -> None:
    _token_env(tmp_path, monkeypatch)
    sent: list[str] = []
    denied = {**PREPARED, "gate_preview": {"decision": "DENY", "reason": "kill switch is enabled"}}
    monkeypatch.setattr(main, "prepare_moodle_live_handoff", lambda **_kw: denied)
    monkeypatch.setattr(notify_mod, "notify", lambda message: sent.append(message) or True)
    response = TestClient(main.app).post(
        "/api/moodle/remediation/ev-cand/prepare?notify=false", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200 and len(sent) == 1 and "CHẶN" in sent[0]


def test_safety_challenge_endpoint_requires_auth_and_never_dispatches(tmp_path, monkeypatch) -> None:
    _token_env(tmp_path, monkeypatch)
    sent: list[str] = []
    calls: list[str] = []
    result = {
        'scenario_id': 'CON-01', 'incident_id': 'inc-1', 'candidate_id': 'ev-cand',
        'dangerous': {'action': 'flush_all_iptables_rules', 'decision': 'DENY'},
        'policy_fault': {'decision': 'DENY'},
        'reviewed': {'decision': 'REQUIRE_APPROVAL', 'action': 'start_reviewed_compose_service', 'scope': 'moodle-app-b'},
        'execution_permitted': False,
    }
    monkeypatch.setattr(main, 'prepare_moodle_live_handoff', lambda **_kw: PREPARED)
    monkeypatch.setattr(main, 'evaluate_challenges', lambda *_a, **_kw: calls.append('shadow') or result)
    monkeypatch.setattr(main, 'execute_moodle_live_handoff', lambda **_kw: pytest.fail('must not dispatch'))
    monkeypatch.setattr(notify_mod, 'notify', lambda message: sent.append(message) or True)
    client = TestClient(main.app)
    assert client.post('/api/moodle/remediation/ev-cand/safety-challenges').status_code == 401
    assert not calls and not sent
    response = client.post('/api/moodle/remediation/ev-cand/safety-challenges', headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 200 and calls == ['shadow'] and len(sent) == 1
    assert response.json()['execution_permitted'] is False and response.json()['telegram_delivered'] is True
    assert 'không phải AI' in sent[0]
    monkeypatch.setattr(notify_mod, 'notify', lambda _message: False)
    response = client.post('/api/moodle/remediation/ev-cand/safety-challenges', headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 200 and response.json()['telegram_delivered'] is False
    assert response.json()['dangerous']['decision'] == 'DENY'


def test_execute_endpoint_reports_rejection_without_secrets(tmp_path, monkeypatch) -> None:
    _token_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AIOPS_SAFE_ACTION_LIVE_ENABLED", "true")
    sent: list[str] = []

    def stale(**_kw):
        raise ValueError("stale")

    monkeypatch.setattr(main, "execute_moodle_live_handoff", stale)
    monkeypatch.setattr(notify_mod, "notify", lambda message: sent.append(message) or True)
    body = {
        "request": PREPARED["request"],
        "gate_approval": {
            "action_sha256": "a" * 64,
            "actor_id": "safety-officer",
            "expires_at": "2099-01-01T00:00:00Z",
            "signature": SIGNATURE,
        },
        "executor_approval": {"actor_id": "operator", "signature": SIGNATURE},
    }

    response = TestClient(main.app).post(
        "/api/moodle/remediation/ev-cand/execute", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 409
    assert len(sent) == 1 and "⑤ YÊU CẦU THỰC THI BỊ TỪ CHỐI" in sent[0]
    assert SIGNATURE not in sent[0] and TOKEN not in sent[0]


def test_execute_endpoint_sends_step_five(tmp_path, monkeypatch) -> None:
    _token_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AIOPS_SAFE_ACTION_LIVE_ENABLED", "true")
    sent: list[str] = []
    monkeypatch.setattr(main, "execute_moodle_live_handoff", lambda **_kw: EXECUTED)
    monkeypatch.setattr(notify_mod, "notify", lambda message: sent.append(message) or True)
    body = {
        "request": PREPARED["request"],
        "gate_approval": {
            "action_sha256": "a" * 64,
            "actor_id": "safety-officer",
            "expires_at": "2099-01-01T00:00:00Z",
            "signature": SIGNATURE,
        },
        "executor_approval": {"actor_id": "operator", "signature": SIGNATURE},
    }

    response = TestClient(main.app).post(
        "/api/moodle/remediation/ev-cand/execute", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    assert len(sent) == 1 and "đã thực thi" in sent[0] and SIGNATURE not in sent[0]


def test_deny_card_lists_all_failed_rules_with_hints() -> None:
    preview = {
        "decision": "DENY",
        "reason": "at least three distinct evidence references are required",
        "failed_rules": [
            {"rule_id": "G04", "class": "REMEDIABLE", "status": "fail", "observed": "2", "required": ">= 3"},
            {"rule_id": "G14", "class": "REMEDIABLE", "status": "fail", "observed": "0.50", "required": ">= 0.65"},
        ],
    }
    text = notify_mod.format_prepare("ev-cand", {**PREPARED, "gate_preview": preview})
    assert "Luật không đạt (2)" in text
    assert "G04 [KHẮC PHỤC ĐƯỢC]" in text and "probe read-only" in text
    assert "G14 [KHẮC PHỤC ĐƯỢC]" in text and "/prepare lại" in text
    assert "CỨNG" not in text and "không mở phê duyệt" in text


def test_deny_card_flags_hard_rules() -> None:
    preview = {
        "decision": "DENY", "reason": "kill switch is enabled",
        "failed_rules": [{"rule_id": "G01", "class": "HARD", "status": "fail", "observed": "on", "required": "off"}],
    }
    text = notify_mod.format_prepare("ev-cand", {**PREPARED, "gate_preview": preview})
    assert "G01 [CỨNG]" in text and "ngoài thẩm quyền Operator" in text
    assert "không thể đạt bằng bằng chứng mới" in text


def test_prepare_endpoint_notifies_when_candidate_is_unavailable(tmp_path, monkeypatch) -> None:
    _token_env(tmp_path, monkeypatch)
    sent: list[str] = []

    def stale(**_kw):
        raise ValueError("stale")

    monkeypatch.setattr(main, "prepare_moodle_live_handoff", stale)
    monkeypatch.setattr(notify_mod, "notify", lambda message: sent.append(message) or True)
    response = TestClient(main.app).post(
        "/api/moodle/remediation/ev-cand/prepare", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 404
    assert len(sent) == 1 and "không có candidate hợp lệ" in sent[0] and TOKEN not in sent[0]


def test_alert_report_shows_admin_steps_only_without_candidate() -> None:
    report = {
        "incident_id": "inc-3", "status": "investigation_hypothesis",
        "diagnosis": {"hypothesis": "upgrade running", "cause_code": "upgrade_in_progress_human_escalation"},
        "remediation_disposition": {"disposition": "escalated_awaiting_operator_isolation"},
        "remediation_candidate": None,
    }
    steps = ["Không tắt chế độ bảo trì khi upgrade còn chạy.", "Kiểm tra log upgrade.", "Chờ metric về 0.", "Bước 4"]
    text = notify_mod.format_alert_report({"labels": {"alertname": "MoodleMaintenanceModeEnabled"}}, report, steps)
    assert "Admin cần làm:" in text and "1. Không tắt chế độ bảo trì" in text and "Bước 4" not in text
    with_candidate = {**report, "remediation_candidate": {"catalog_action_id": "x", "action": {}}}
    text = notify_mod.format_alert_report({"labels": {}}, with_candidate, steps)
    assert "Admin cần làm:" not in text
