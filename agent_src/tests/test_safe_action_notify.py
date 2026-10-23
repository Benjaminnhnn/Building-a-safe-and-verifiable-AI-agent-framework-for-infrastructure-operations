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
}
EXECUTED = {
    "status": "executed",
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


def test_prepare_message_names_gate_and_hides_signatures() -> None:
    text = notify_mod.format_prepare("ev-cand", PREPARED)
    assert text.startswith("④ Safety Gate")
    assert "CON-01" in text and "moodle-app-b" in text and "2 chữ ký" in text
    assert "a" * 64 not in text  # only a hash prefix is shown


def test_execute_message_distinguishes_executed_from_blocked() -> None:
    assert notify_mod.format_execute("ev-cand", EXECUTED).startswith("⑤ Executor: đã thực thi")
    blocked = {**EXECUTED, "status": "blocked", "mutated": False, "gate": {"decision": "DENY", "reason": "expired"}}
    text = notify_mod.format_execute("ev-cand", blocked)
    assert "KHÔNG thực thi" in text and "expired" in text


def test_verification_message_counts_passing_samples() -> None:
    text = notify_mod.format_verification("req-1", PAYLOAD, {"status": "resolved", "event_hash": "c" * 64})
    assert text.startswith("⑥ Independent Verifier: RESOLVED")
    assert "4/5" in text


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
    assert len(sent) == 1 and sent[0].startswith("④")


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
    assert len(sent) == 1 and sent[0].startswith("⑤") and "bị từ chối" in sent[0]
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
