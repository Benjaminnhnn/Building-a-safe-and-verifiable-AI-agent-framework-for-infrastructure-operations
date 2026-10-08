from unittest.mock import patch

from fastapi.testclient import TestClient

from core import main


client = TestClient(main.app)


def _payload(fingerprint: str = "alert-1") -> dict:
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "WebEndpointDown",
                    "instance": "bank-web-01",
                },
                "annotations": {},
                "startsAt": "2026-06-02T00:00:00Z",
                "generatorURL": "http://prometheus",
                "fingerprint": fingerprint,
            }
        ],
    }


def test_webhook_enqueues_alert_when_queue_has_capacity() -> None:
    with (
        patch.object(main.redis_client, "llen", return_value=2),
        patch.object(main.redis_client, "set", return_value=True),
        patch.object(main.process_alerts_task, "delay") as delay,
    ):
        response = client.post("/webhook", json=_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "enqueued"
    assert response.json()["queue_depth"] == 3
    delay.assert_called_once()
    queued_payload = delay.call_args.args[0]
    event = queued_payload["alerts"][0]
    assert event["schema_version"] == "2.0"
    assert event["source"] == "alertmanager"
    assert event["event_type"] == "service_health_failed"
    assert event["correlation_id"].startswith("corr-")


def test_webhook_skips_duplicate_before_enqueue() -> None:
    with (
        patch.object(main.redis_client, "llen", return_value=1000),
        patch.object(main.redis_client, "set", return_value=False),
        patch.object(main.process_alerts_task, "delay") as delay,
    ):
        response = client.post("/webhook", json=_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "deduped"
    delay.assert_not_called()


def test_webhook_returns_503_when_queue_is_full() -> None:
    with (
        patch.object(main.redis_client, "llen", return_value=1000),
        patch.object(main.redis_client, "set", return_value=True),
        patch.object(main.redis_client, "delete") as delete,
        patch.object(main.process_alerts_task, "delay") as delay,
    ):
        response = client.post("/webhook", json=_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "Celery queue is at capacity"
    delete.assert_called_once_with("alert-ingress-cooldown:alert-1")
    delay.assert_not_called()


def test_webhook_rejects_missing_or_invalid_bearer_token(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "alertmanager-token"
    token_file.write_text("expected-token\n", encoding="utf-8")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_TOKEN_FILE", str(token_file))

    missing = client.post("/webhook", json=_payload("missing-token"))
    invalid = client.post(
        "/webhook",
        json=_payload("invalid-token"),
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert "expected-token" not in missing.text + invalid.text


def test_webhook_auth_fails_closed_when_required_token_file_is_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_TOKEN_FILE", str(tmp_path / "missing-token"))

    response = client.post("/webhook", json=_payload("missing-file"))

    assert response.status_code == 503
    assert "token" not in response.text.lower()


def test_webhook_accepts_alertmanager_bearer_token(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "alertmanager-token"
    token_file.write_text("expected-token\n", encoding="utf-8")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_AUTH_REQUIRED", "true")
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_TOKEN_FILE", str(token_file))

    with (
        patch.object(main.redis_client, "llen", return_value=0),
        patch.object(main.redis_client, "set", return_value=True),
        patch.object(main.process_alerts_task, "delay") as delay,
    ):
        response = client.post(
            "/webhook",
            json=_payload("valid-token"),
            headers={"Authorization": "Bearer expected-token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "enqueued"
    delay.assert_called_once()


def test_webhook_auth_required_without_token_configuration_returns_503(monkeypatch) -> None:
    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_AUTH_REQUIRED", "true")
    monkeypatch.delenv("ALERTMANAGER_WEBHOOK_TOKEN_FILE", raising=False)

    response = client.post("/webhook", json=_payload("unconfigured-token"))

    assert response.status_code == 503
