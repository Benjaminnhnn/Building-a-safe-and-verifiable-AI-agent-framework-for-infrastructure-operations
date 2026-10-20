"""Tests for MoodlePlatformAdapter.

Verifies:
- Resource identification and alias resolution
- Inventory loading and resource models
- PromQL queries by alert
- Diagnosis and remediation planning delegation
- Action catalog and target scopes
- Verification probes and read-only target client
"""

from __future__ import annotations

from core.safety.action_catalog import ActionCatalog
from core.schema.common import Environment
from core.schema.resource import Resource
from core.evidence.evidence_collectors import METRIC_QUERIES_BY_ALERT
from core.moodle.adapter import (
    MOODLE_ALERT_RESOURCE_MAP,
    RESOURCE_ALIASES,
    MoodlePlatformAdapter,
)
from core.platform_adapter import PlatformAdapterRegistry


def test_moodle_adapter_registration_and_metadata() -> None:
    adapter = PlatformAdapterRegistry.get("moodle")
    assert adapter is not None
    assert isinstance(adapter, MoodlePlatformAdapter)
    assert adapter.platform_name == "moodle"


def test_moodle_adapter_can_handle() -> None:
    adapter = MoodlePlatformAdapter()

    # Alert name prefix
    assert adapter.can_handle({"labels": {"alertname": "MoodleNodeExporterDown"}})
    assert adapter.can_handle({"labels": {"alertname": "MoodlePublicProbeFailed"}})

    # Service name
    assert adapter.can_handle({"labels": {"alertname": "CustomAlert", "service": "moodle"}})
    assert adapter.can_handle({"labels": {"alertname": "CustomAlert", "service": "moodle-rds"}})
    assert adapter.can_handle({"labels": {"alertname": "CustomAlert", "service": "postgres-db"}})

    # Explicit alert map
    assert adapter.can_handle({"labels": {"alertname": "LogFileErrorDetected"}})

    # Non-Moodle alerts
    assert not adapter.can_handle({"labels": {"alertname": "OrderPaymentFailed", "service": "payments"}})
    assert not adapter.can_handle({"labels": {"alertname": "KubePodCrashLoop", "service": "k8s"}})


def test_moodle_adapter_identify_resource_via_alert_map() -> None:
    adapter = MoodlePlatformAdapter()

    for alert_name, expected_resource in MOODLE_ALERT_RESOURCE_MAP.items():
        alert = {"labels": {"alertname": alert_name}}
        resolved = adapter.identify_resource(alert)
        assert resolved == expected_resource, f"Expected {expected_resource} for {alert_name}, got {resolved}"


def test_moodle_adapter_identify_resource_via_aliases_and_labels() -> None:
    adapter = MoodlePlatformAdapter()

    # Via service alias
    assert adapter.identify_resource({"labels": {"alertname": "GenericAlert", "service": "moodle"}}) == "moodle-app"
    assert adapter.identify_resource({"labels": {"alertname": "GenericAlert", "service": "moodle-rds"}}) == "postgres-db"
    assert adapter.identify_resource({"labels": {"alertname": "GenericAlert", "service": "moodle-efs"}}) == "moodledata-volume"

    # Via target alias
    assert adapter.identify_resource({"labels": {"alertname": "GenericAlert", "target": "moodle-rds"}}) == "postgres-db"

    # Unknown
    assert adapter.identify_resource({"labels": {"alertname": "GenericAlert", "service": "unknown-service"}}) is None


def test_moodle_adapter_resources_inventory() -> None:
    adapter = MoodlePlatformAdapter()
    resources = adapter.get_resources()

    assert len(resources) > 0
    resource_ids = {r.resource_id for r in resources}
    assert "moodle-app" in resource_ids
    assert "postgres-db" in resource_ids
    assert "moodledata-volume" in resource_ids
    assert "moodle-web-endpoint" in resource_ids

    # Check typed Resource instances
    app_resource = adapter.get_resource("moodle-app")
    assert app_resource is not None
    assert isinstance(app_resource, Resource)
    assert app_resource.resource_id == "moodle-app"
    assert app_resource.environment in {Environment.STAGING, Environment.PRODUCTION}

    assert adapter.get_resource("nonexistent-resource-id") is None

    # Check resource types mapping
    types = adapter.get_resource_types()
    assert types["moodle-app"] == "application"
    assert types["postgres-db"] == "database"
    assert types["moodledata-volume"] == "volume"
    assert types["moodle-web-endpoint"] == "endpoint"


def test_moodle_adapter_alert_queries() -> None:
    adapter = MoodlePlatformAdapter()

    for alert_name, queries in METRIC_QUERIES_BY_ALERT.items():
        adapter_queries = adapter.get_alert_queries(alert_name)
        assert adapter_queries == queries

    assert adapter.get_alert_queries("NonexistentAlert") == ()


def test_moodle_adapter_action_catalog_and_target_scopes() -> None:
    adapter = MoodlePlatformAdapter()

    catalog = adapter.get_action_catalog()
    assert isinstance(catalog, ActionCatalog)
    assert catalog.lookup("remove_scoped_db_reject") is not None
    assert catalog.lookup("reconcile_sec02_safe_baseline") is not None
    assert catalog.lookup("verify_tls_database_connection") is not None

    scopes = adapter.get_target_scopes()
    assert isinstance(scopes, dict)
    assert "staging_moodle_nodes" in scopes
    assert isinstance(scopes["staging_moodle_nodes"], set)


def test_moodle_adapter_diagnosis_and_planning_delegation() -> None:
    adapter = MoodlePlatformAdapter()

    # Diagnosis with empty evidence produces hypothesis or requires evidence
    diag = adapter.diagnose("MoodleNodeCpuHigh", [])
    assert isinstance(diag, dict)
    assert "status" in diag

    # Investigation planning with an isolated hypothesis
    diag_hypothesis = {
        "status": "hypothesis",
        "cause_code": "rds_path_probe_failed",
        "evidence_refs": ["ev-rds-1"],
    }
    inv_plan = adapter.plan_investigation("inc-test-1", diag_hypothesis)
    assert inv_plan is not None
    assert getattr(inv_plan, "target_resource_id", None) == "postgres-db"

    # S3.3 Remediation planning
    remediation = adapter.plan_remediation("inc-test-1", "MoodleNodeCpuHigh", diag, [])
    # May be None or dict depending on preconditions/scenario
    assert remediation is None or isinstance(remediation, dict)


def test_moodle_adapter_verification_probes() -> None:
    adapter = MoodlePlatformAdapter()
    # Verification probes
    probes = adapter.get_verification_probes("inc-test-1")
    assert "allowed_probes" in probes
    assert "forbidden_probes" in probes
    assert "related_probes" in probes
    assert len(probes["allowed_probes"]) > 0
    assert len(probes["forbidden_probes"]) > 0
    assert len(probes["related_probes"]) > 0
