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
