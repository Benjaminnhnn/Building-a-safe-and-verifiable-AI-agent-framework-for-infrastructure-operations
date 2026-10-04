from __future__ import annotations

from core.auth_alert_pipeline import is_auth_alert, process_auth_alert
from core.unified_core import SQLiteEvidenceStore


def _alert(*, name="AuthLabOpenLdapUnavailable", status="firing"):
    return {
        "status": status,
        "labels": {
            "alertname": name,
            "scenario": "AUTH-01",
            "environment": "local-auth-lab",
        },
        "annotations": {"summary": "LDAPS probe failed", "password": "must-not-be-recorded"},
        "fingerprint": "auth-fingerprint",
        "startsAt": "2026-10-04T00:00:00Z",
    }


def test_auth_alert_is_detected_without_forwarding_ground_truth() -> None:
    event = _alert()
    assert is_auth_alert(event)
    assert "expected_root_cause" not in event


def test_auth01_live_probe_pattern_denies_action_and_persists_redacted_evidence(tmp_path) -> None:
    values = {
        'probe_success{job="openldap_ldaps"}': 0,
        'probe_success{job="moodle"}': 1,
        "auth_lab_valid_login_success": 0,
    }
    report = process_auth_alert(
        _alert(), metric_reader=values.__getitem__, evidence_path=tmp_path / "evidence.db"
    )

    assert report["status"] == "ESCALATE"
    assert report["action_decision"] == "DENY"
    assert report["resolution_eligible"] is False
    assert "directory-path failure is consistent" in report["diagnosis"]
    records = SQLiteEvidenceStore(tmp_path / "evidence.db").query(incident_id=report["incident_id"])
    assert len(records) == 2
    assert all(record.payload.get("password") is None for record in records)
    assert all("must-not-be-recorded" not in str(record.payload) for record in records)


def test_login_failure_with_ldap_port_up_is_not_overdiagnosed(tmp_path) -> None:
    values = {
        'probe_success{job="openldap_ldaps"}': 1,
        'probe_success{job="moodle"}': 1,
        "auth_lab_valid_login_success": 0,
    }
    report = process_auth_alert(
        _alert(name="AuthLabSyntheticLdapLoginFailed"),
        metric_reader=values.__getitem__,
        evidence_path=tmp_path / "evidence.db",
    )

    assert "cause is undetermined" in report["diagnosis"]
    assert report["action_decision"] == "DENY"


def test_resolved_alert_is_only_a_recovery_signal_and_never_resolves(tmp_path) -> None:
    values = {
        'probe_success{job="openldap_ldaps"}': 1,
        'probe_success{job="moodle"}': 1,
        "auth_lab_valid_login_success": 1,
    }
    report = process_auth_alert(
        _alert(status="resolved"),
        metric_reader=values.__getitem__,
        evidence_path=tmp_path / "evidence.db",
    )

    assert report["status"] == "RECOVERY_SIGNAL_ONLY"
    assert report["resolution_eligible"] is False
    records = SQLiteEvidenceStore(tmp_path / "evidence.db").query(
        incident_id=report["incident_id"], kind="auth_probe_snapshot"
    )
    assert records[0].payload["disposition"] == "RECOVERY_SIGNAL_ONLY"


def test_unknown_auth_scenario_is_fail_closed(tmp_path) -> None:
    event = _alert()
    event["labels"]["scenario"] = "AUTH-99"
    values = {
        'probe_success{job="openldap_ldaps"}': 0,
        'probe_success{job="moodle"}': 1,
        "auth_lab_valid_login_success": 0,
    }
    report = process_auth_alert(
        event, metric_reader=values.__getitem__, evidence_path=tmp_path / "evidence.db"
    )
    assert report["action_decision"] == "DENY"
    assert "not in the observed local AUTH-01 contract" in report["diagnosis"]
