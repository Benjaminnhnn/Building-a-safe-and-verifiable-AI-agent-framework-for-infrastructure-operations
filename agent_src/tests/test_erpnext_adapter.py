"""Tests for ERPNextPlatformAdapter demonstrating multi-platform architecture."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.erpnext.adapter import ERPNextPlatformAdapter
from core.platform_adapter import PlatformAdapter, PlatformAdapterRegistry
from core.shadow_pipeline import run_shadow_if_enabled


def test_erpnext_adapter_satisfies_protocol_and_registers() -> None:
    adapter = ERPNextPlatformAdapter()
    assert isinstance(adapter, PlatformAdapter)
    assert adapter.platform_name == "erpnext"

    registered = PlatformAdapterRegistry.get("erpnext")
    assert registered is not None
    assert registered.platform_name == "erpnext"


def test_erpnext_adapter_alert_matching_and_resource_resolution() -> None:
    adapter = ERPNextPlatformAdapter()

    mariadb_alert = {
        "fingerprint": "fp-erp-db-1",
        "labels": {"alertname": "ERPNextMariaDBDown", "service": "erpnext", "environment": "staging"},
    }
    assert adapter.can_handle(mariadb_alert) is True
    assert adapter.identify_resource(mariadb_alert) == "erpnext-mariadb"
    assert PlatformAdapterRegistry.find_for_alert(mariadb_alert) is not None

    moodle_alert = {
        "fingerprint": "fp-moodle-1",
        "labels": {"alertname": "MoodleRdsTcpProbeFailed", "service": "moodle"},
    }
    assert adapter.can_handle(moodle_alert) is False


def test_erpnext_dependency_graph_tracks_downstream_impact() -> None:
    adapter = ERPNextPlatformAdapter()
    graph = adapter.get_dependency_graph()

    downstream = graph.downstream("erpnext-mariadb")
    assert "erpnext-app" in downstream
    assert "erpnext-nginx" in downstream
    assert "erpnext-web-endpoint" in downstream
    assert graph.impact("erpnext-mariadb")[0] == "erpnext-mariadb"


def test_erpnext_shadow_pipeline_processes_alerts_without_core_changes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(tmp_path / "erpnext-shadow.sqlite"))
    adapter = ERPNextPlatformAdapter()

    for alert_name, expected_action, expected_resource in (
        ("ERPNextMariaDBDown", "start_erpnext_mariadb_container", "erpnext-mariadb"),
        ("ERPNextRedisQueueDown", "start_erpnext_redis_container", "erpnext-redis-queue"),
        ("ERPNextNginxUpstreamDrift", "restore_erpnext_nginx_config", "erpnext-nginx"),
    ):
        alert = {
            "status": "firing",
            "fingerprint": f"fp-{alert_name}",
            "labels": {
                "alertname": alert_name,
                "service": "erpnext",
                "environment": "staging",
            },
            "annotations": {"summary": f"Simulated {alert_name}"},
        }
        result = run_shadow_if_enabled(alert, adapter=adapter)
        assert result is not None
        assert result["status"] == "observed"
        assert result["confirmed_affected_resources"] == [expected_resource]
        assert result["execution_permitted"] is False
        assert result["resolution_eligible"] is False
        assert result["remediation_candidate"] is not None
        assert result["remediation_candidate"]["catalog_action_id"] == expected_action
        assert result["remediation_candidate"]["execution_permitted"] is False
        assert "erpnext-web-endpoint" in result["potentially_affected_resources"]
        assert result["evidence_provenance"]
        assert result["explanation"]["hypothesis"]
        assert result["explanation"]["reason"]
