from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.auth_alert_pipeline import (
    AUTH_PROBES,
    attempt_auth01_recovery,
    recovery_gate_reason,
)
from core import tasks
from core.unified_core import SQLiteEvidenceStore


def _alert(**overrides):
    labels = {
        "alertname": "AuthLabOpenLdapUnavailable",
        "scenario": "AUTH-01",
        "environment": "local-auth-lab",
        "evidence_class": "runtime",
        "resource_id": "authlab-openldap",
    }
    alert = {"status": "firing", "labels": labels, "fingerprint": "sample"}
    for key, value in overrides.items():
        if key == "labels":
            alert["labels"].update(value)
        else:
            alert[key] = value
    return alert


def _reader(values=None):
    samples = {
        'probe_success{job="openldap_ldaps"}': 0,
        'probe_success{job="moodle"}': 1,
        "auth_lab_valid_login_success": 0,
    }
    if values:
        samples.update(values)
    return samples.__getitem__


def _verdict(**overrides):
    duration = overrides.get("stability_seconds", 120)
    count = overrides.get("observations", 9)
    checked_at = datetime.now(timezone.utc)
    stability_observations = [
        {
            "observed_at": checked_at - timedelta(
                seconds=duration - duration * index / max(1, count - 1)
            ),
            "healthy": True,
            "simulated": overrides.get("simulated", True),
        }
        for index in range(count)
    ]
    result = {
        "authority": "independent_verifier",
        "simulated": overrides.get("simulated", True),
        "valid_login_passed": True,
        "invalid_login_denied": True,
        "health_passed": True,
        "ldaps_passed": True,
        "stability_seconds": duration,
        "observations": count,
        "stability_observations": stability_observations,
        "resolution_eligible": True,
    }
    result.update(overrides)
    return result


def test_recovery_gate_requires_exact_runtime_contract() -> None:
    assert recovery_gate_reason(_alert(), {
        "ldap_ldaps": 0,
        "valid_login": 0,
        "moodle_health": 1,
    }, enabled=True) is None
    assert recovery_gate_reason(_alert(labels={"environment": "production"}), {
        "ldap_ldaps": 0,
        "valid_login": 0,
        "moodle_health": 1,
    }, enabled=True) == "environment is not the local auth lab"
    assert recovery_gate_reason(_alert(), {
        "ldap_ldaps": 0,
        "valid_login": 0,
        "moodle_health": 0,
    }, enabled=True) == "Moodle health is not passing"
    assert recovery_gate_reason(_alert(), {
        "ldap_ldaps": 0,
        "valid_login": 0,
        "moodle_health": 1,
    }, enabled=False) == "automatic local recovery is disabled"


def test_auto_recovery_requires_flag_and_all_live_gates(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AUTH_LAB_AUTO_RECOVERY_ENABLED", "true")
    called = []
    report = attempt_auth01_recovery(
        _alert(labels={"resource_id": "other-service"}),
        metric_reader=_reader(),
        actuator=lambda: called.append("actuator") or {"target": "local-openldap"},
        verifier=lambda: called.append("verifier") or _verdict(),
        evidence_path=tmp_path / "evidence.db",
    )
    assert report is None
    assert called == []


def test_independent_verifier_is_the_only_resolution_authority(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AUTH_LAB_AUTO_RECOVERY_ENABLED", "true")
    report = attempt_auth01_recovery(
        _alert(),
        metric_reader=_reader(),
        actuator=lambda: {"target": "local-openldap", "operation": "start", "health": "healthy"},
        verifier=lambda: _verdict(simulated=False),
        evidence_path=tmp_path / "evidence.db",
    )

    assert report["status"] == "RESOLVED"
    assert report["action_decision"] == "ALLOW"
    assert report["resolution_eligible"] is True
    records = SQLiteEvidenceStore(tmp_path / "evidence.db").query(
        incident_id=report["incident_id"]
    )
    transitions = [record.payload for record in records if record.kind == "auth_incident_transition"]
    assert transitions[-1]["new_state"] == "RESOLVED"
    assert transitions[-1]["authority"] == "independent_verifier"
    assert all(item["authority"] != "independent_verifier" or item["new_state"] == "RESOLVED" for item in transitions)


def test_fixture_verifier_result_cannot_resolve_auth_incident(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AUTH_LAB_AUTO_RECOVERY_ENABLED", "true")
    report = attempt_auth01_recovery(
        _alert(),
        metric_reader=_reader(),
        actuator=lambda: {"target": "local-openldap", "operation": "start", "health": "healthy"},
        verifier=_verdict,
        evidence_path=tmp_path / "evidence.db",
    )

    assert report["status"] == "ESCALATED"
    assert report["verifier"]["simulated"] is True
    assert report["resolution_eligible"] is False
    records = SQLiteEvidenceStore(tmp_path / "evidence.db").query(
        incident_id=report["incident_id"]
    )
    transitions = [record.payload for record in records if record.kind == "auth_incident_transition"]
    assert transitions[-1]["new_state"] == "ESCALATED"


def test_short_or_failed_verifier_keeps_incident_unresolved(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AUTH_LAB_AUTO_RECOVERY_ENABLED", "true")
    report = attempt_auth01_recovery(
        _alert(),
        metric_reader=_reader(),
        actuator=lambda: {"target": "local-openldap", "operation": "restart", "health": "healthy"},
        verifier=lambda: _verdict(stability_seconds=45),
        evidence_path=tmp_path / "evidence.db",
    )

    assert report["status"] == "ESCALATED"
    assert report["resolution_eligible"] is False
    records = SQLiteEvidenceStore(tmp_path / "evidence.db").query(
        incident_id=report["incident_id"]
    )
    transitions = [record.payload for record in records if record.kind == "auth_incident_transition"]
    assert transitions[-1]["new_state"] == "ESCALATED"
    assert not any(item["new_state"] == "RESOLVED" for item in transitions)


def test_actuator_failure_never_calls_verifier(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AUTH_LAB_AUTO_RECOVERY_ENABLED", "true")
    called = []

    def fail_action():
        raise RuntimeError("actuator unavailable")

    report = attempt_auth01_recovery(
        _alert(),
        metric_reader=_reader(),
        actuator=fail_action,
        verifier=lambda: called.append("verifier") or _verdict(),
        evidence_path=tmp_path / "evidence.db",
    )

    assert report["status"] == "ESCALATED"
    assert report["action_status"] == "FAILED"
    assert report["resolution_eligible"] is False
    assert called == []


def test_recovery_gate_metric_contract_has_only_fixed_queries() -> None:
    assert set(AUTH_PROBES) == {"ldap_ldaps", "moodle_health", "valid_login"}


def test_redis_context_does_not_downgrade_independent_verifier_resolution(monkeypatch) -> None:
    class MemoryRedis:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def setex(self, key, ttl, value):
            self.values[key] = value

    memory_redis = MemoryRedis()
    memory_redis.setex(
        "incident:auth-incident",
        100,
        '{"scenario":"AUTH-01","status":"RESOLVED",'
        '"resolution_authority":"independent_verifier"}',
    )
    monkeypatch.setattr(tasks, "redis_client", memory_redis)

    tasks.save_incident_to_redis(
        "auth-incident", {"scenario": "AUTH-01", "status": "ESCALATE"}
    )

    assert json.loads(memory_redis.get("incident:auth-incident"))["status"] == "RESOLVED"
