from __future__ import annotations

import asyncio

from core import tasks
from core.moodle.alert_correlation import con01_dispatch_for_alert
from core.safety.safe_action_audit import SafeActionAuditStore
from core.shadow_pipeline import _stable_id


def _alert():
    return {"status": "resolved", "fingerprint": "fp-con01", "startsAt": "2026-11-07T06:00:00Z",
            "labels": {"alertname": "MoodleWebContainerMissing", "instance": "moodle-app-b", "environment": "staging"}}


def test_only_exact_con01_dispatch_is_correlated(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.sqlite3"
    monkeypatch.setenv("AIOPS_SAFE_ACTION_AUDIT_DB", str(audit_path))
    alert = _alert()
    incident = _stable_id("inc", alert["fingerprint"], alert["startsAt"])
    with SafeActionAuditStore(audit_path) as audit:
        audit.append(incident_id=incident, event_type="live_dispatch_started", payload={
            "request_id": "req-1", "scenario_id": "CON-01", "target_scope": "moodle-app-b"})
    assert con01_dispatch_for_alert(alert) == (incident, "req-1")
    assert con01_dispatch_for_alert({**alert, "fingerprint": "another"}) is None
    assert con01_dispatch_for_alert({**alert, "startsAt": "another"}) is None
    assert con01_dispatch_for_alert({**alert, "labels": {**alert["labels"], "instance": "moodle-app-a"}}) is None
    assert con01_dispatch_for_alert({**alert, "labels": {**alert["labels"], "alertname": "MoodleNodeMemoryPressure"}}) is None


def test_matching_resolution_is_deferred_but_independent_alert_is_sent(monkeypatch):
    sent, scheduled = [], []
    monkeypatch.setattr(tasks, "_clear_alert_cooldown", lambda _: None)
    monkeypatch.setattr(tasks, "_reserve_alert_notification", lambda *_: True)
    monkeypatch.setattr(tasks, "con01_dispatch_for_alert", lambda alert: ("inc", "req") if alert["labels"]["alertname"] == "MoodleWebContainerMissing" else None)
    monkeypatch.setattr(tasks.deferred_con01_resolved, "apply_async", lambda **kwargs: scheduled.append(kwargs))
    monkeypatch.setattr(tasks, "send_telegram_message", lambda message, **_: sent.append(message) or True)
    asyncio.run(tasks.process_single_alert(_alert()))
    other = _alert()
    other["labels"]["alertname"] = "MoodleNodeMemoryPressure"
    asyncio.run(tasks.process_single_alert(other))
    assert len(scheduled) == 1 and scheduled[0]["countdown"] == 600
    assert len(sent) == 1 and "MoodleNodeMemoryPressure" in sent[0]


def test_deferred_alert_falls_back_unless_verified_notice_delivered(monkeypatch):
    sent = []
    monkeypatch.setattr(tasks, "send_telegram_message", lambda message, **_: sent.append(message) or True)
    monkeypatch.setattr(tasks, "verification_resolved", lambda *_: True)
    monkeypatch.setattr(tasks, "was_delivered", lambda *_: False)
    assert tasks.deferred_con01_resolved.run(_alert(), "inc", "req") == "fallback_sent"
    assert len(sent) == 1
    monkeypatch.setattr(tasks, "was_delivered", lambda *_: True)
    assert tasks.deferred_con01_resolved.run(_alert(), "inc", "req") == "included_in_verifier_summary"
    assert len(sent) == 1
