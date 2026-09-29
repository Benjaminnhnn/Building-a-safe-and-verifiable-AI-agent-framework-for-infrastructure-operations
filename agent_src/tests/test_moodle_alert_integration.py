from __future__ import annotations

import asyncio

import pytest

from core import tasks
from core.moodle_alert_integration import process_moodle_alert
from core.moodle_contract import load_moodle_ground_truth

_MOODLE_SCENARIOS = load_moodle_ground_truth()


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
    assert len(report["evidence_refs"]) == 4
    assert report["scenario_binding"]["execution_lane"] == "staging_live_allowlisted"
    assert report["scenario_binding"]["live_allowlisted_action_count"] == 2
    assert report["scenario_binding"]["execution_permitted_by_binding"] is False
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_shadow_integration_marks_unenabled_scenario_offline_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))

    report = process_moodle_alert(_alert("DB-02"))

    assert report["status"] == "shadow_complete"
    assert report["scenario_binding"]["execution_lane"] == "offline_or_shadow_only"
    assert report["scenario_binding"]["execution_permitted_by_binding"] is False
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


@pytest.mark.parametrize(
    ("scenario_id", "alert_name"),
    [
        (scenario_id, scenario["observed_signals"][0])
        for scenario_id, scenario in sorted(_MOODLE_SCENARIOS.items())
    ],
    ids=sorted(_MOODLE_SCENARIOS),
)
def test_all_ground_truth_scenarios_stay_shadow_only(
    tmp_path, monkeypatch, scenario_id: str, alert_name: str
) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / f"{scenario_id}.sqlite3"))
    monkeypatch.setattr(
        "core.moodle_alert_integration._route_live",
        lambda *_args, **_kwargs: pytest.fail("shadow flow must never route to live"),
    )

    report = process_moodle_alert(_alert(scenario_id, alert_name))

    assert report["status"] == "shadow_complete", scenario_id
    assert report["scenario_id"] == scenario_id
    assert report["scenario_binding"]["execution_permitted_by_binding"] is False
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False
    assert report["stages"][-2]["stage"] == "execution"
    assert report["stages"][-2]["status"] == "ok"
    assert report["stages"][-1] == {"stage": "verification", "status": "ok"}


def test_shadow_integration_fails_closed_on_signal_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))

    report = process_moodle_alert(_alert(alert_name="UnrelatedAlert"))

    assert report["status"] == "escalated"
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_shadow_integration_rejects_untyped_scenario_action_before_planning(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    database_path = tmp_path / "evidence.sqlite3"
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(database_path))
    invalid_truth = dict(_MOODLE_SCENARIOS["DB-01"])
    invalid_truth["allowed_remediation"] = [
        {"action": "unrestricted_shell", "target": "staging_moodle_nodes"}
    ]
    monkeypatch.setattr(
        "core.moodle_alert_integration._load_catalog",
        lambda: {"DB-01": invalid_truth},
    )

    report = process_moodle_alert(_alert())

    assert report["status"] == "escalated"
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False
    assert not database_path.exists()


def test_shadow_integration_without_scenario_delegates_to_generic_observer(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")

    alert = _alert()
    del alert["labels"]["scenario_id"]

    # tasks.process_single_alert() routes this value to run_shadow_if_enabled,
    # which records resource-mapped evidence without selecting a remediation.
    assert process_moodle_alert(alert) is None


def test_live_mode_routes_to_pipeline_not_raises(tmp_path, monkeypatch) -> None:
    """Live mode must no longer raise RuntimeError; it should attempt pipeline routing.

    Without a real Moodle environment the pipeline call will fail gracefully and
    return an escalated report — but crucially it must NOT raise RuntimeError.
    """
    from unittest.mock import patch
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "live")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))

    # Patch _route_live so it returns a controlled escalated dict (avoids subprocess calls)
    escalated = {
        "status": "escalated",
        "scenario_id": "DB-01",
        "reason": "alert normalization failed (test stub)",
        "execution_permitted": False,
        "resolution_eligible": False,
    }
    with patch("core.moodle_alert_integration._route_live", return_value=escalated):
        report = process_moodle_alert(_alert())

    assert report is not None
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


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
