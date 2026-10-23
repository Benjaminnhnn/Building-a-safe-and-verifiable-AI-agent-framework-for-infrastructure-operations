from __future__ import annotations

from fastapi.testclient import TestClient

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


def test_prepare_message_lists_each_gate_check_and_hides_full_hash() -> None:
    text = notify_mod.format_prepare("ev-cand", PREPARED)
    assert "④ SAFETY GATE" in text
    assert text.count("✅") == 8 and "confidence 0.65 < 0.80" in text
    assert "CHƯA ĐƯỢC THỰC THI" in text and "2 chữ ký" in text
    assert "a" * 64 not in text  # only a hash prefix is shown


def test_prepare_message_reports_a_gate_denial() -> None:
    denied = {**PREPARED, "gate_preview": {"decision": "DENY", "reason": "kill switch is enabled"}}
    text = notify_mod.format_prepare("ev-cand", denied)
    assert "BỊ CHẶN: kill switch is enabled" in text and "✅" not in text


def test_execute_message_reads_the_executor_result_not_the_workflow_status() -> None:
    # The workflow reports awaiting_verification; the executor result says executed.
    assert "⑤ EXECUTOR ĐÃ THỰC THI" in notify_mod.format_execute("ev-cand", EXECUTED)
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
    assert "4/5" in text


def test_alert_report_for_candidate_says_ai_will_not_execute() -> None:
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
    assert "[CON-01]" in text and "8 bằng chứng" in text
    assert "reconcile_con01_safe_baseline" in text and "AI KHÔNG tự thực thi" in text


def test_alert_report_for_human_only_has_no_proposal_to_execute() -> None:
    report = {
        "incident_id": "inc-2",
        "status": "investigation_hypothesis",
        "diagnosis": {"hypothesis": "SSO option missing", "cause_code": "sso_login_option_missing", "confidence": 0.65},
        "remediation_disposition": {"disposition": "human_only_escalation", "scenario_id": "SSO-13",
                                    "catalog_action_id": "restore_moodle_oauth2_issuer",
                                    "target_scope": "staging_moodle_oauth2_issuer"},
        "remediation_candidate": None,
    }
    text = notify_mod.format_alert_report({"labels": {"alertname": "MoodleSsoLoginOptionMissing"}}, report)
    assert "CHỈ NGƯỜI ĐƯỢC SỬA" in text and "restore_moodle_oauth2_issuer" in text
    assert "ĐỀ XUẤT KHẮC PHỤC" not in text


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
    assert len(sent) == 1 and "ĐÃ THỰC THI" in sent[0] and SIGNATURE not in sent[0]
