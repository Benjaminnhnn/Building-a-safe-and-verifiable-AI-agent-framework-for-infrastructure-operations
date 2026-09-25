import asyncio
import json
from unittest.mock import Mock, patch

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


def test_resolved_alert_clears_firing_cooldown_without_resending_resolved() -> None:
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
        patch.object(tasks, "get_rag_instance", return_value=None),
        patch.object(tasks, "run_agent_workflow", return_value=("analysis", {"action": "fix", "host": "bank-web-01"})),
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
        patch.object(tasks, "save_incident_to_redis"),
        patch.object(tasks.verify_resolution_task, "apply_async"),
    ):
        asyncio.run(tasks.process_single_alert(firing_alert))
        asyncio.run(tasks.process_single_alert(firing_alert))
        asyncio.run(tasks.process_single_alert(resolved_alert))
        asyncio.run(tasks.process_single_alert(resolved_alert))
        asyncio.run(tasks.process_single_alert(refired_alert))

    assert send_telegram_message.call_count == 3


def test_resolved_alert_marks_only_matching_incident_for_verification() -> None:
    alert = _alert("same-alert")
    alert["status"] = "resolved"
    alert["endsAt"] = "2026-06-04T02:05:00Z"
    context = {"alert_name": "WebEndpointDown", "status": "firing"}
    redis_client = Mock()
    redis_client.get.side_effect = ["incident-1", json.dumps(context)]

    with (
        patch.object(tasks, "redis_client", redis_client),
        patch.object(tasks, "save_incident_to_redis") as save_incident_to_redis,
    ):
        incident_id = tasks._mark_matching_incident_recovery_signal(alert)

    assert incident_id == "incident-1"
    saved_context = save_incident_to_redis.call_args.args[1]
    assert saved_context["status"] == "verification_pending"
    assert saved_context["recovery_signal_at"] == "2026-06-04T02:05:00Z"
    assert saved_context["recovery_signal_source"] == "alertmanager"
    redis_client.delete.assert_not_called()


def test_verification_checks_incident_after_alertmanager_recovery_signal() -> None:
    redis_client = Mock()
    redis_client.get.return_value = json.dumps(
        {
            "alert_name": "WebEndpointDown",
            "instance": "bank-web-01",
            "status": "verification_pending",
        }
    )

    with (
        patch.object(tasks, "redis_client", redis_client),
        patch.object(tasks, "check_alert_resolved", return_value=True) as check_alert_resolved,
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
        patch.object(tasks, "get_rag_instance", return_value=None) as get_rag_instance,
    ):
        asyncio.run(tasks.verify_resolution("incident-1", "WebEndpointDown", "bank-web-01"))

    check_alert_resolved.assert_called_once()
    send_telegram_message.assert_called_once()
    get_rag_instance.assert_called_once()
    redis_client.delete.assert_called_once_with("incident:incident-1")


def test_verification_keeps_failed_incident_context_for_admin_feedback() -> None:
    redis_client = Mock()
    redis_client.get.return_value = json.dumps(
        {
            "alert_name": "WebEndpointDown",
            "instance": "bank-web-01",
            "status": "firing",
            "incident_details": "web endpoint is down",
            "ai_analysis": "check frontend container",
            "proposal": {"action": "start_frontend"},
        }
    )

    with (
        patch.object(tasks, "redis_client", redis_client),
        patch.object(tasks, "check_alert_resolved", return_value=False),
        patch.object(tasks, "send_telegram_message"),
        patch.object(tasks, "get_rag_instance", return_value=None),
    ):
        asyncio.run(tasks.verify_resolution("incident-1", "WebEndpointDown", "bank-web-01"))

    redis_client.delete.assert_not_called()


def test_verification_deletes_resolved_incident_context() -> None:
    redis_client = Mock()
    redis_client.get.return_value = json.dumps(
        {
            "alert_name": "WebEndpointDown",
            "instance": "bank-web-01",
            "status": "firing",
            "incident_details": "web endpoint is down",
            "ai_analysis": "check frontend container",
            "proposal": {"action": "start_frontend"},
        }
    )

    with (
        patch.object(tasks, "redis_client", redis_client),
        patch.object(tasks, "check_alert_resolved", return_value=True),
        patch.object(tasks, "send_telegram_message"),
        patch.object(tasks, "get_rag_instance", return_value=None),
    ):
        asyncio.run(tasks.verify_resolution("incident-1", "WebEndpointDown", "bank-web-01"))

    redis_client.delete.assert_called_once_with("incident:incident-1")


def test_web_endpoint_alert_context_includes_probe_target() -> None:
    details = tasks.build_incident_details(_alert("web-down"))

    assert "Alert: WebEndpointDown" in details
    assert "Instance: bank-web-01" in details
    assert "Job: blackbox_http_web" in details
    assert "Target: http://52.220.34.44/health" in details


def test_web_endpoint_diagnosis_has_actionable_checks() -> None:
    analysis, proposal = tasks.deterministic_diagnosis(_alert("web-down"))

    assert "frontend-web-prod" in analysis
    assert "docker ps -a --filter name=frontend-web-prod" in analysis
    assert proposal == {
        "action": "check_or_start_frontend_web_prod",
        "host": "bank-web-01",
    }


def test_staging_web_endpoint_diagnosis_uses_staging_container_and_port() -> None:
    alert = _runbook_alert("WebEndpointDown", "frontend-web-staging", "bank-web-01")
    alert["labels"]["target"] = "http://13.228.171.39:18081/health"

    analysis, proposal = tasks.deterministic_diagnosis(alert)

    assert "frontend-web-staging" in analysis
    assert "http://127.0.0.1:18081/health" in analysis
    assert proposal == {
        "action": "check_or_start_frontend_web_staging",
        "host": "bank-web-01",
    }


def test_frontend_api_proxy_diagnosis_prioritizes_upstream_configuration() -> None:
    alert = _runbook_alert("FrontendAPIProxyDown", "frontend-web-staging", "bank-web-01")
    alert["labels"]["target"] = "http://13.250.87.160:18081/api/ready"
    alert["labels"]["dependency"] = "payment-api-staging"
    alert["labels"]["expected_upstream"] = "http://10.10.1.119:18080"

    analysis, proposal = tasks.deterministic_diagnosis(alert)

    assert "PAYMENT_API_UPSTREAM" in analysis
    assert "frontend /health vẫn trả 200" in analysis
    assert "PAYMENT_API_UPSTREAM=http://10.10.1.119:18080" in analysis
    assert "Payment API readiness probe trực tiếp vẫn khỏe" in analysis
    assert proposal == {
        "action": "fix_frontend_web_staging_api_upstream",
        "host": "bank-web-01",
    }


def test_payment_api_endpoint_diagnosis_includes_network_checks() -> None:
    alert = _runbook_alert("PaymentAPIEndpointDown", "payment-api-staging", "bank-core-01")
    alert["labels"]["target"] = "http://10.10.1.119:18080/api/ready"

    analysis, proposal = tasks.deterministic_diagnosis(alert)

    assert "Security Group, firewall hoặc route" in analysis
    assert "iptables" in analysis
    assert proposal == {
        "action": "restore_payment_api_staging_endpoint",
        "host": "bank-core-01",
    }


def test_high_cpu_diagnosis_uses_host_resource_checks() -> None:
    alert = _runbook_alert("HighCPUUsage", "", "bank-core-01")

    analysis, proposal = tasks.deterministic_diagnosis(alert)

    assert "top -o %CPU" in analysis
    assert "docker stats --no-stream" in analysis
    assert proposal == {
        "action": "reduce_cpu_usage",
        "host": "bank-core-01",
    }


def test_postgresql_diagnosis_has_core_redeploy_steps() -> None:
    analysis, proposal = tasks.deterministic_diagnosis(
        _runbook_alert("PostgreSQLDown", "postgres-staging", "bank-core-01")
    )

    assert "postgres-staging" in analysis
    assert "pg_isready -U aiops_user -d aiops_db" in analysis
    assert "./automation/app-release-deploy.sh staging \"$TAG\" core" in analysis
    assert proposal == {
        "action": "check_or_start_postgres_staging",
        "host": "bank-core-01",
    }


def test_redis_diagnosis_has_monitor_redeploy_steps() -> None:
    analysis, proposal = tasks.deterministic_diagnosis(
        _runbook_alert("RedisDown", "redis-cache-staging", "monitor-ai-01")
    )

    assert "redis-cache-staging" in analysis
    assert "redis-cli ping" in analysis
    assert "./automation/app-release-deploy.sh staging \"$TAG\" monitor" in analysis
    assert proposal == {
        "action": "check_or_start_redis_cache_staging",
        "host": "monitor-ai-01",
    }


def test_docker_container_diagnosis_maps_component_to_role() -> None:
    analysis, proposal = tasks.deterministic_diagnosis(
        _runbook_alert("DockerContainerDown", "payment-api-staging", "bank-core-01")
    )

    assert "payment-api-staging" in analysis
    assert "Deploy role: core" in analysis
    assert "./automation/app-release-deploy.sh staging \"$TAG\" core" in analysis
    assert proposal == {
        "action": "redeploy_core_staging",
        "host": "bank-core-01",
    }


def test_known_runbook_alert_does_not_call_gemini() -> None:
    alert = _runbook_alert("RedisDown", "redis-cache-staging", "monitor-ai-01")

    with (
        patch.object(tasks, "redis_client", None),
        patch.object(tasks, "ALERT_DEDUP_ENABLED", False),
        patch.object(tasks, "get_rag_instance", return_value=None),
        patch.object(tasks, "run_agent_workflow") as run_agent_workflow,
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
        patch.object(tasks, "save_incident_to_redis"),
        patch.object(tasks.verify_resolution_task, "apply_async"),
    ):
        asyncio.run(tasks.process_single_alert(alert))

    run_agent_workflow.assert_not_called()
    assert send_telegram_message.called
    assert "redis-cache-staging" in send_telegram_message.call_args.args[0]


def test_known_runbook_alert_keeps_rag_context_out_of_telegram_report() -> None:
    alert = _runbook_alert("RedisDown", "redis-cache-staging", "monitor-ai-01")
    fake_rag = Mock()
    fake_rag.query_knowledge.return_value = "previous admin-reviewed Redis solution"

    with (
        patch.object(tasks, "redis_client", None),
        patch.object(tasks, "ALERT_DEDUP_ENABLED", False),
        patch.object(tasks, "get_rag_instance", return_value=fake_rag),
        patch.object(tasks, "send_telegram_message") as send_telegram_message,
        patch.object(tasks, "save_incident_to_redis") as save_incident,
        patch.object(tasks.verify_resolution_task, "apply_async"),
    ):
        asyncio.run(tasks.process_single_alert(alert))

    fake_rag.query_knowledge.assert_called_once_with(
        tasks.build_incident_details(alert),
        alert_name="RedisDown",
    )
    context = save_incident.call_args.args[1]
    assert context["rag_context"] == "previous admin-reviewed Redis solution"
    assert "previous admin-reviewed Redis solution" not in context["ai_analysis"]
    assert "previous admin-reviewed Redis solution" not in send_telegram_message.call_args.args[0]


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
