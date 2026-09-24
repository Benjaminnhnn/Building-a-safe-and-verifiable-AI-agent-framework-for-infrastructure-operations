from __future__ import annotations

import asyncio

import pytest

from core import tasks
from core.moodle_alert_integration import process_moodle_alert


def _alert(scenario_id: str = "DB-01", alert_name: str = "MoodleSyntheticTransactionFailed") -> dict:
    return {
        "status": "firing",
        "event_id": "evt-integration-1",
        "fingerprint": "moodle-integration-1",
        "labels": {"alertname": alert_name, "scenario_id": scenario_id, "environment": "staging", "service": "moodle", "severity": "critical"},
        "annotations": {"summary": "staging synthetic check failed"},
    }


def test_shadow_integration_replays_ordered_stages_without_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))

    report = process_moodle_alert(_alert())

    assert report["status"] == "shadow_complete"
    assert [stage["stage"] for stage in report["stages"]] == ["observer", "diagnosis", "planner", "gate", "execution", "verification"]
    assert len(report["evidence_refs"]) == 3
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_shadow_integration_fails_closed_on_signal_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))

    report = process_moodle_alert(_alert(alert_name="UnrelatedAlert"))

    assert report["status"] == "escalated"
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_shadow_integration_rejects_live_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "live")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))
    with pytest.raises(RuntimeError, match="live execution is not supported"):
        process_moodle_alert(_alert())


def test_celery_worker_routes_staging_moodle_alert_to_unified_pipeline(monkeypatch) -> None:
    report = {"status": "shadow_complete", "scenario_id": "DB-01", "incident_id": "i1", "execution_permitted": False, "resolution_eligible": False}
    monkeypatch.setattr(tasks, "_reserve_alert_processing", lambda _alert: True)
    monkeypatch.setattr(tasks, "_reserve_alert_notification", lambda _alert, _kind: True)
    monkeypatch.setattr(tasks, "process_moodle_alert", lambda _alert: report)
    monkeypatch.setattr(tasks, "send_telegram_message", lambda *args, **kwargs: True)

    asyncio.run(tasks.process_single_alert(_alert()))


def test_alertmanager_resolved_moodle_alert_does_not_resolve_incident(monkeypatch) -> None:
    resolved = _alert()
    resolved["status"] = "resolved"
    monkeypatch.setattr(tasks, "_clear_alert_cooldown", lambda _alert: None)
    monkeypatch.setattr(tasks, "_mark_matching_incident_resolved", lambda _alert: pytest.fail("Alertmanager must not resolve Moodle incidents"))
    monkeypatch.setattr(tasks, "_reserve_alert_notification", lambda _alert, _kind: True)
    monkeypatch.setattr(tasks, "send_telegram_message", lambda *args, **kwargs: True)

    asyncio.run(tasks.process_single_alert(resolved))
