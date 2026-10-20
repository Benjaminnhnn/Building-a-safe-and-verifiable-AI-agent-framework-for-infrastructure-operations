import asyncio
from unittest.mock import Mock, patch

import pytest

from core import tasks


def _alert(fingerprint: str | None = None) -> dict:
    alert = {
        "status": "firing",
        "labels": {
            "alertname": "WebEndpointDown",
            "instance": "bank-web-01",
            "job": "blackbox_http_web",
            "service": "availability",
            "target": "http://52.220.34.44/health",
        },
        "annotations": {},
        "startsAt": "2026-06-04T02:00:00Z",
    }
    if fingerprint:
        alert["fingerprint"] = fingerprint
    return alert


def _runbook_alert(alertname: str, component: str, instance: str) -> dict:
    return {
        "status": "firing",
        "labels": {
            "alertname": alertname,
            "instance": instance,
            "job": "demo",
            "service": "demo",
            "component": component,
            "environment": "staging",
        },
        "annotations": {"summary": f"{component} is down"},
        "startsAt": "2026-06-04T02:00:00Z",
    }


def test_alert_identity_prefers_alertmanager_fingerprint() -> None:
    assert tasks._alert_identity(_alert("abc123")) == "abc123"


def test_alert_processing_uses_local_cooldown_when_redis_unavailable() -> None:
    tasks._local_alert_cooldowns.clear()

    alert = _alert("same-alert")

    with (
        patch.object(tasks, "redis_client", None),
        patch.object(tasks, "ALERT_DEDUP_ENABLED", True),
        patch.object(tasks, "ALERT_AI_COOLDOWN_SECONDS", 60),
    ):
        assert tasks._reserve_alert_processing(alert) is True
        assert tasks._reserve_alert_processing(alert) is False

        tasks._clear_alert_cooldown(alert)
        assert tasks._reserve_alert_processing(alert) is True


def test_failed_moodle_processing_releases_reservations_for_celery_retry(monkeypatch) -> None:
    alert = _runbook_alert("MoodleSyntheticTransactionFailed", "moodle", "node-a")
    alert["labels"]["service"] = "moodle"
    alert["labels"]["environment"] = "staging"
    alert["startsAt"] = "2026-10-06T00:00:00Z"
    tasks._local_alert_cooldowns.clear()
    tasks._local_alert_notifications.clear()
    monkeypatch.setattr(tasks, "redis_client", None)
    monkeypatch.setattr(tasks, "ALERT_DEDUP_ENABLED", True)
    monkeypatch.setattr(tasks, "ALERT_AI_COOLDOWN_SECONDS", 60)
    monkeypatch.setattr(tasks, "ALERT_NOTIFICATION_TTL_SECONDS", 60)
    metric_reports = []
    monkeypatch.setattr(tasks, "_record_moodle_pipeline_event", metric_reports.append)

    def fail(_alert):
        raise RuntimeError("collector unavailable")

    monkeypatch.setattr(tasks, "process_moodle_alert", fail)
    with pytest.raises(RuntimeError, match="collector unavailable"):
        asyncio.run(tasks.process_single_alert(alert))

    assert tasks._reserve_alert_processing(alert) is True
    assert tasks._reserve_alert_notification(alert, "firing") is True
    assert metric_reports == [{"status": "error"}]


def test_resolved_notification_is_sent_once_per_alert_event() -> None:
    tasks._local_alert_notifications.clear()

    alert = _alert("same-alert")
    alert["status"] = "resolved"

    with (
        patch.object(tasks, "redis_client", None),
        patch.object(tasks, "ALERT_DEDUP_ENABLED", True),
        patch.object(tasks, "ALERT_NOTIFICATION_TTL_SECONDS", 60),
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
    ):
        asyncio.run(tasks.process_single_alert(alert))
        asyncio.run(tasks.process_single_alert(alert))

    send_telegram_message.assert_called_once()


def test_non_moodle_alerts_do_not_enter_legacy_incident_assistant() -> None:
    tasks._local_alert_cooldowns.clear()
    tasks._local_alert_notifications.clear()

    firing_alert = _alert("same-alert")
    resolved_alert = _alert("same-alert")
    resolved_alert["status"] = "resolved"
    refired_alert = _alert("same-alert")
    refired_alert["startsAt"] = "2026-06-04T02:10:00Z"

    with (
        patch.object(tasks, "redis_client", None),
        patch.object(tasks, "ALERT_DEDUP_ENABLED", True),
        patch.object(tasks, "ALERT_AI_COOLDOWN_SECONDS", 60),
        patch.object(tasks, "ALERT_NOTIFICATION_TTL_SECONDS", 60),
        patch.object(tasks.genai, "Client") as gemini_client,
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
        patch.object(tasks, "process_moodle_alert", return_value=None),
    ):
        asyncio.run(tasks.process_single_alert(firing_alert))
        asyncio.run(tasks.process_single_alert(firing_alert))
        asyncio.run(tasks.process_single_alert(resolved_alert))
        asyncio.run(tasks.process_single_alert(resolved_alert))
        asyncio.run(tasks.process_single_alert(refired_alert))

    gemini_client.assert_not_called()
    assert send_telegram_message.call_count == 1






























def test_unknown_non_moodle_alert_is_unsupported_without_fallback() -> None:
    alert = _runbook_alert("RedisDown", "redis-cache-staging", "monitor-ai-01")

    with (
        patch.object(tasks, "redis_client", None),
        patch.object(tasks, "ALERT_DEDUP_ENABLED", False),
        patch.object(tasks.genai, "Client") as gemini_client,
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
        patch.object(tasks, "process_moodle_alert", return_value=None),
    ):
        asyncio.run(tasks.process_single_alert(alert))

    gemini_client.assert_not_called()
    send_telegram_message.assert_not_called()


def test_non_moodle_alert_does_not_query_rag_or_save_an_incident() -> None:
    alert = _runbook_alert("RedisDown", "redis-cache-staging", "monitor-ai-01")
    fake_rag = Mock()

    with (
        patch.object(tasks, "redis_client", None),
        patch.object(tasks, "ALERT_DEDUP_ENABLED", False),
        patch.object(tasks, "get_rag_instance", return_value=fake_rag),
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
        patch.object(tasks.genai, "Client") as gemini_client,
        patch.object(tasks, "process_moodle_alert", return_value=None),
    ):
        asyncio.run(tasks.process_single_alert(alert))

    fake_rag.query_knowledge.assert_not_called()
    gemini_client.assert_not_called()
    send_telegram_message.assert_not_called()


def test_retired_generic_incident_paths_are_not_in_runtime() -> None:
    assert not hasattr(tasks, "run_agent_workflow")
    assert "verify_resolution_task" not in tasks.celery_app.tasks








def test_alert_batch_concurrency_is_bounded() -> None:
    active = 0
    peak = 0

    async def fake_process_single_alert(alert: dict) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    with (
        patch.object(tasks, "ALERT_BATCH_CONCURRENCY", 2),
        patch.object(tasks, "process_single_alert", side_effect=fake_process_single_alert),
    ):
        tasks.process_alerts_task.run({"alerts": [{"id": i} for i in range(6)]})

    assert peak == 2
