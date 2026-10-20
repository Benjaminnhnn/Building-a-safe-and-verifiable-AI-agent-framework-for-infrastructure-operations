from __future__ import annotations

from fastapi.testclient import TestClient

from core.main import app


def test_live_handoff_prepare_requires_separate_bearer_token(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "safe-action-token"
    token_file.write_text("x" * 64, encoding="ascii")
    public_key = tmp_path / "gate-public.pem"
    public_key.write_text("not-read-until-prepare", encoding="ascii")
    monkeypatch.setenv("AIOPS_GATE_APPROVAL_PUBLIC_KEY_FILE", str(public_key))
    monkeypatch.setenv("AIOPS_SAFE_ACTION_API_TOKEN_FILE", str(token_file))

    response = TestClient(app).post(
        "/api/moodle/remediation/ev-candidate/prepare",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_live_handoff_execute_is_disabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_SAFE_ACTION_LIVE_ENABLED", "false")
    request = {
        "request_id": "req-test",
        "idempotency_key": "idem-test",
        "catalog_action_id": "remove_scoped_db_reject",
        "scenario_id": "DB-01",
        "target_scope": "staging_moodle_nodes",
        "action": {
            "action_id": "act-test",
            "incident_id": "inc-test",
            "action_type": "remove_scoped_port_block",
            "target_resource_id": "postgres-db",
            "environment": "staging",
            "reason": "test only",
            "evidence_refs": ["ev-1", "ev-2", "ev-3"],
            "expected_outcome": "healthy",
            "reversible": True,
            "rollback_plan": {
                "available": True,
                "rollback_action_id": "reconcile_db01_safe_baseline",
                "method": "reapply baseline",
            },
            "requires_approval": True,
        },
    }
    body = {
        "request": request,
        "gate_approval": {
            "action_sha256": "0" * 64,
            "actor_id": "safety-officer",
            "expires_at": "2099-01-01T00:00:00Z",
            "signature": "ed25519:invalid",
        },
        "executor_approval": {
            "actor_id": "operator",
            "expires_at": "2099-01-01T00:00:00Z",
            "action_sha256": "0" * 64,
            "signature": "invalid",
        },
    }

    response = TestClient(app).post(
        "/api/moodle/remediation/ev-candidate/execute", json=body
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "safe live action path is disabled"
