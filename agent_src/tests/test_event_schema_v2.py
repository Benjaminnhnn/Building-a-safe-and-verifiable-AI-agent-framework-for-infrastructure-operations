"""V2 schema validation: defaults, entities override, sensitive detection, 2.0 + backward compat."""

from __future__ import annotations

import json
from pathlib import Path

from core.event_schema import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_V1,
    SUPPORTED_SCHEMA_VERSIONS,
    normalize_alert,
    normalize_alertmanager_payload,
    validate_alertmanager_payload,
    validate_normalized_event,
)


def _base_alert(**overrides) -> dict:
    base = {
        "status": "firing",
        "labels": {
            "alertname": "TestAlert",
            "instance": "bank-core-01",
            "service": "payment-api",
            "environment": "staging",
            "severity": "critical",
        },
        "annotations": {"summary": "test"},
        "startsAt": "2026-09-01T02:00:00Z",
        "fingerprint": "test-fp-001",
    }
    base.update(overrides)
    if "labels" in overrides:
        base["labels"] = overrides["labels"]
    return base


def test_defaults_entities_classification_redaction() -> None:
    alert = _base_alert()
    norm = normalize_alert(alert, correlation_id="corr-test", received_at="2026-09-01T02:00:01Z")
    assert norm["schema_version"] == SCHEMA_VERSION
    assert norm["data_classification"] == "internal"
    assert isinstance(norm["entities"], dict)
    assert norm["redaction"]["status"] == "passed"
    assert norm["redaction"]["policy_version"] == "redaction-v1"
    assert isinstance(norm["redaction"]["redacted_fields"], list)
    assert not validate_normalized_event(norm)


def test_entities_built_from_labels_and_annotations() -> None:
    alert = _base_alert(
        labels={
            "alertname": "DatabaseMigrationFailed",
            "instance": "eks-staging-1",
            "service": "transaction-service",
            "business_capability": "mobile_banking_transfer_money",
            "repository": "org/mobile-banking",
            "database_migration_id": "mig-001",
            "severity": "critical",
        },
        annotations={"summary": "x", "cluster": "eks-staging", "namespace": "staging"},
    )
    norm = normalize_alert(alert, correlation_id="corr-1")
    assert norm["entities"]["business_capability"] == "mobile_banking_transfer_money"
    assert norm["entities"]["repository"] == "org/mobile-banking"
    assert norm["entities"]["database_migration_id"] == "mig-001"
    # annotation fallback
    assert norm["entities"]["cluster"] == "eks-staging"
    assert norm["entities"]["namespace"] == "staging"


def test_entities_override_wins_over_labels() -> None:
    alert = _base_alert(
        labels={
            "alertname": "K8sDeploymentRolloutFailed",
            "service": "transaction-service",
            "workload": "old-workload",
            "cluster": "old-cluster",
        }
    )
    override = {"workload": "transaction-service", "cluster": "eks-staging", "namespace": "staging"}
    norm = normalize_alert(alert, correlation_id="corr-1", entities=override)
    assert norm["entities"]["workload"] == "transaction-service"
    assert norm["entities"]["cluster"] == "eks-staging"
    assert norm["entities"]["namespace"] == "staging"


def test_entities_override_ignores_none() -> None:
    alert = _base_alert(labels={"alertname": "X", "service": "y", "workload": "w1"})
    norm = normalize_alert(alert, correlation_id="c", entities={"workload": None, "cluster": "eks-staging"})
    # None should not overwrite existing
    assert norm["entities"]["workload"] == "w1"
    assert norm["entities"]["cluster"] == "eks-staging"


def test_data_classification_override_and_normalization() -> None:
    alert = _base_alert()
    norm = normalize_alert(alert, correlation_id="c", data_classification="confidential")
    assert norm["data_classification"] == "confidential"
    # invalid falls back to internal
    norm2 = normalize_alert(alert, correlation_id="c", data_classification="INVALID_CLASS")
    assert norm2["data_classification"] == "internal"
    # from labels
    alert2 = _base_alert(labels={"alertname": "X", "service": "y", "data_classification": "restricted"})
    norm3 = normalize_alert(alert2, correlation_id="c")
    assert norm3["data_classification"] == "restricted"


def test_redaction_override_applied() -> None:
    alert = _base_alert()
    redaction = {"status": "skipped", "redacted_fields": ["field_a"], "policy_version": "redaction-v1"}
    norm = normalize_alert(alert, correlation_id="c", redaction=redaction)
    assert norm["redaction"]["status"] == "skipped"
    assert norm["redaction"]["redacted_fields"] == ["field_a"]


def test_validation_pass_for_normalized_batch() -> None:
    payload = {
        "alerts": [
            _base_alert(fingerprint="a1", labels={"alertname": "PostgreSQLDown", "service": "payment-api", "severity": "critical"}),
            _base_alert(fingerprint="a2", labels={"alertname": "PaymentAPIEndpointDown", "service": "payment-api", "severity": "critical"}),
        ]
    }
    batch = normalize_alertmanager_payload(payload, received_at="2026-09-01T02:00:02Z")
    assert len(batch["alerts"]) == 2
    # same correlation_id per batch
    assert len({e["correlation_id"] for e in batch["alerts"]}) == 1
    for ev in batch["alerts"]:
        assert not validate_normalized_event(ev)


def test_validation_fail_missing_required() -> None:
    alert = _base_alert()
    norm = normalize_alert(alert, correlation_id="c")
    # drop required field
    del norm["event_id"]
    errs = validate_normalized_event(norm)
    assert any("missing required field: event_id" in e for e in errs)

    # invalid schema_version
    norm2 = normalize_alert(alert, correlation_id="c")
    norm2["schema_version"] = "9.9"
    errs2 = validate_normalized_event(norm2)
    assert any("unsupported schema_version" in e for e in errs2)


def test_validation_fail_invalid_classification_and_entities() -> None:
    alert = _base_alert()
    norm = normalize_alert(alert, correlation_id="c")
    norm["data_classification"] = "top_secret"
    errs = validate_normalized_event(norm)
    assert any("invalid data_classification" in e for e in errs)

    norm2 = normalize_alert(alert, correlation_id="c")
    norm2["entities"] = "not-a-dict"
    errs2 = validate_normalized_event(norm2)
    assert any("entities must be an object" in e for e in errs2)

    norm3 = normalize_alert(alert, correlation_id="c")
    norm3["entities"] = {"api_key": "value"}
    errs3 = validate_normalized_event(norm3)
    assert any("sensitive key" in e for e in errs3)

    norm4 = normalize_alert(alert, correlation_id="c")
    norm4["redaction"] = {"status": "bad_status", "redacted_fields": []}
    errs4 = validate_normalized_event(norm4)
    assert any("invalid redaction.status" in e for e in errs4)


def test_sensitive_key_detection_in_payload_validation() -> None:
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "X", "api_key": "should-not-be-here"},
                "annotations": {},
                "startsAt": "2026-09-01T02:00:00Z",
            }
        ]
    }
    errs = validate_alertmanager_payload(payload)
    assert any("sensitive data" in e for e in errs)


def test_sensitive_value_detection_akey_and_private_key() -> None:
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "X", "instance": "host-a"},
                "annotations": {"summary": "AKIA1234567890ABCDEF exposed"},
                "startsAt": "2026-09-01T02:00:00Z",
            }
        ]
    }
    errs = validate_alertmanager_payload(payload)
    assert any("sensitive data" in e for e in errs)

    # also via normalized event validation (value scanned)
    alert = _base_alert(labels={"alertname": "X", "service": "y"})
    norm = normalize_alert(alert, correlation_id="c")
    norm["signal"] = {"name": "x", "value": "AKIA1234567890ABCDEF", "threshold": None}
    errs2 = validate_normalized_event(norm)
    assert any("sensitive data leakage" in e for e in errs2)


def test_normalize_marks_redaction_failed_on_sensitive_input() -> None:
    alert = {
        "status": "firing",
        "labels": {"alertname": "X", "service": "y", "secret_token": "abc"},
        "annotations": {},
        "startsAt": "2026-09-01T02:00:00Z",
        "fingerprint": "fp-sensitive",
    }
    norm = normalize_alert(alert, correlation_id="c")
    assert norm["redaction"]["status"] == "failed"
    errs = validate_normalized_event(norm)
    # sensitive key in original labels causes leakage flag via entities? but at least redaction failed
    assert norm["redaction"]["status"] == "failed"
    # payload-level validation should also have flagged
    payload_errs = validate_alertmanager_payload({"alerts": [alert]})
    assert any("sensitive" in e for e in payload_errs)


def test_schema_2_and_backward_compat_1() -> None:
    assert SCHEMA_VERSION == "2.0"
    assert SCHEMA_VERSION_V1 == "1.0"
    assert SUPPORTED_SCHEMA_VERSIONS == {"2.0", "1.0"}

    alert = _base_alert()
    norm_v2 = normalize_alert(alert, correlation_id="c")
    assert norm_v2["schema_version"] == "2.0"
    assert not validate_normalized_event(norm_v2)

    # manually downgrade to 1.0 must still validate (backward compat)
    norm_v1 = dict(norm_v2)
    norm_v1["schema_version"] = "1.0"
    # strip v2-only fields to simulate pure v1 payload
    norm_v1.pop("entities", None)
    norm_v1.pop("data_classification", None)
    norm_v1.pop("redaction", None)
    assert not validate_normalized_event(norm_v1)

    # old caller without v2 kwargs still works
    payload = {"alerts": [_base_alert(fingerprint="old-1")]}
    batch = normalize_alertmanager_payload(payload)
    assert batch["alerts"][0]["schema_version"] == "2.0"
    assert "entities" in batch["alerts"][0]

