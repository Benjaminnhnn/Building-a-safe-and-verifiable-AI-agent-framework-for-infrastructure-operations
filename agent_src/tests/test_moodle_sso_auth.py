"""SSO-13: the agent diagnoses a missing Okta sign-in option but never executes a fix."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.evidence.evidence_collectors import METRIC_QUERIES_BY_ALERT
from core.moodle.diagnosis import diagnose_moodle_alert
from core.moodle.planner import (
    AUTH_REMEDIATION_DISPOSITION,
    describe_moodle_remediation_disposition,
    plan_moodle_s3_3_remediation,
)
from core.safety.action_catalog import ActionCatalog

ALERT = "MoodleSsoLoginOptionMissing"
REPO = Path(__file__).resolve().parents[2]


def _evidence(query_id: str, value: float, evidence_id: str, *, age_seconds: int = 0):
    return SimpleNamespace(
        source="prometheus",
        evidence_id=evidence_id,
        collected_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        metadata={"payload_json": json.dumps({
            "query_id": query_id,
            "sample_value": value,
            "labels": {"instance": "blackbox-exporter:9115"},
        })},
    )


def _observations(*, sso: float, page: float, age_seconds: int = 0):
    return [
        _evidence("moodle_sso_login_option", sso, "ev-sso", age_seconds=age_seconds),
        _evidence("moodle_login_page_probe", page, "ev-login-page", age_seconds=age_seconds),
    ]


def test_missing_sso_option_on_healthy_login_page_is_an_identity_hypothesis() -> None:
    result = diagnose_moodle_alert(ALERT, _observations(sso=0.0, page=1.0))

    assert result["status"] == "hypothesis"
    assert result["cause_code"] == "sso_login_option_missing"
    assert result["evidence_refs"] == ["ev-login-page", "ev-sso"]
    assert result["action_candidate"] is None


@pytest.mark.parametrize(
    ("sso", "page"),
    [(1.0, 1.0), (0.0, 0.0), (1.0, 0.0)],
)
def test_sso_alert_conflicts_unless_option_fails_while_page_passes(sso: float, page: float) -> None:
    result = diagnose_moodle_alert(ALERT, _observations(sso=sso, page=page))

    assert result["status"] == "conflicting_evidence"
    assert result["hypothesis"] is None


def test_sso_alert_needs_both_fresh_probes() -> None:
    only_signal = diagnose_moodle_alert(ALERT, [_evidence("moodle_sso_login_option", 0.0, "ev-sso")])
    stale = diagnose_moodle_alert(ALERT, _observations(sso=0.0, page=1.0, age_seconds=600))

    assert only_signal["status"] == "insufficient_evidence"
    assert only_signal["missing_queries"] == ["moodle_login_page_probe"]
    assert stale["status"] == "insufficient_evidence"


def test_sso_hypothesis_is_a_human_only_escalation_without_a_candidate() -> None:
    diagnosis = diagnose_moodle_alert(ALERT, _observations(sso=0.0, page=1.0))

    candidate = plan_moodle_s3_3_remediation(
        incident_id="inc-sso", alert_name=ALERT, diagnosis=diagnosis, evidence=[]
    )
    disposition = describe_moodle_remediation_disposition(
        alert_name=ALERT, diagnosis=diagnosis, remediation_candidate=None
    )

    assert candidate is None
    assert disposition["disposition"] == "human_only_escalation"
    assert disposition["scenario_id"] == "SSO-13"
    assert disposition["catalog_action_id"] == "restore_moodle_oauth2_issuer"
    assert disposition["execution_permitted"] is False


def test_unconfirmed_sso_diagnosis_does_not_claim_a_human_only_action() -> None:
    disposition = describe_moodle_remediation_disposition(
        alert_name=ALERT,
        diagnosis={"status": "conflicting_evidence", "reason": "probes disagree"},
    )

    assert disposition["disposition"] == "awaiting_valid_evidence"
    assert disposition["catalog_action_id"] is None


def test_oauth2_issuer_action_is_operator_only_and_never_executable() -> None:
    action_id = AUTH_REMEDIATION_DISPOSITION[ALERT]["catalog_action_id"]
    entry = ActionCatalog.load_moodle_catalog().lookup(action_id)

    assert entry is not None
    assert entry.permission.required_role == "operator"
    assert entry.rollback_action is None
    assert entry.blast_radius == "HIGH"
    assert ActionCatalog.load_moodle_s3_3_execution_catalog().lookup(action_id) is None
    for role in ("executor", "operator"):
        assert not ActionCatalog.load_moodle_catalog().is_safe_execution_allowed(
            action_id,
            scenario_id="SSO-13",
            target_scope="staging_moodle_oauth2_issuer",
            environment="staging",
            role=role,
        )


def test_executor_wrapper_and_api_allowlist_have_no_identity_action() -> None:
    action_id = AUTH_REMEDIATION_DISPOSITION[ALERT]["catalog_action_id"]
    wrapper = (REPO / "ansible/playbooks/configure-moodle-safe-executor.yml").read_text(encoding="utf-8")
    api = (REPO / "automation/moodle-safe-executor-api.py").read_text(encoding="utf-8")

    assert action_id not in wrapper
    assert "SSO-13" not in wrapper
    assert action_id not in api


def test_sso_alert_rule_requires_a_healthy_login_page() -> None:
    rules = (REPO / "ansible/config/moodle_alert_rules.yml").read_text(encoding="utf-8")
    rule = rules.split(f"- alert: {ALERT}", 1)[1].split("- alert:", 1)[0]

    assert 'probe_success{job="blackbox_moodle_sso"} == 0' in rule
    assert 'probe_success{job="blackbox_moodle_public",probe="login"} == 1' in rule
    assert dict(METRIC_QUERIES_BY_ALERT[ALERT]) == {
        "moodle_sso_login_option": 'probe_success{job="blackbox_moodle_sso"}',
        "moodle_login_page_probe": 'probe_success{job="blackbox_moodle_public",probe="login"}',
    }
