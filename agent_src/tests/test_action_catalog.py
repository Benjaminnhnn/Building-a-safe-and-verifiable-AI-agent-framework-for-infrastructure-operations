"""Tests for ActionCatalog and CatalogEntry."""

from __future__ import annotations

import pytest
from core.action_catalog import ActionCatalog


@pytest.fixture()
def catalog() -> ActionCatalog:
    return ActionCatalog.load_moodle_catalog()


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


def test_moodle_catalog_loads_all_actions(catalog: ActionCatalog) -> None:
    entries = list(catalog._entries.values())
    assert len(entries) >= 14, f"Expected at least 14 entries, got {len(entries)}"


def test_lookup_returns_entry(catalog: ActionCatalog) -> None:
    entry = catalog.lookup("remove_scoped_db_reject")
    assert entry is not None
    assert entry.action_id == "remove_scoped_db_reject"


def test_lookup_unknown_returns_none(catalog: ActionCatalog) -> None:
    assert catalog.lookup("unknown_action") is None


# ---------------------------------------------------------------------------
# Permission / environment checks
# ---------------------------------------------------------------------------


def test_staging_only_actions_forbidden_in_production(catalog: ActionCatalog) -> None:
    result = catalog.is_allowed("remove_scoped_db_reject", environment="production", role="executor")
    assert result is False


def test_executor_can_run_staging_action(catalog: ActionCatalog) -> None:
    result = catalog.is_allowed("remove_scoped_db_reject", environment="staging", role="executor")
    assert result is True


def test_read_only_probes_allowed_for_verifier(catalog: ActionCatalog) -> None:
    # verify_tls_database_connection is a read-only probe
    result = catalog.is_allowed("verify_tls_database_connection", environment="staging", role="verifier")
    assert result is True


def test_non_verifier_cannot_use_verifier_probe(catalog: ActionCatalog) -> None:
    # executor does not have read_only probe permission for verifier-scoped probes
    result = catalog.is_allowed("verify_tls_database_connection", environment="staging", role="executor")
    assert result is False


def test_observer_cannot_run_any_action(catalog: ActionCatalog) -> None:
    result = catalog.is_allowed("restart_moodle_container", environment="staging", role="observer")
    assert result is False


# ---------------------------------------------------------------------------
# Blast radius integrity
# ---------------------------------------------------------------------------


def test_all_entries_have_blast_radius(catalog: ActionCatalog) -> None:
    valid = {"LOW", "MEDIUM", "HIGH"}
    for entry in catalog._entries.values():
        assert entry.blast_radius in valid, (
            f"Entry '{entry.action_id}' has invalid blast_radius='{entry.blast_radius}'"
        )


def test_restart_moodle_container_is_medium(catalog: ActionCatalog) -> None:
    entry = catalog.lookup("restart_moodle_container")
    assert entry is not None
    assert entry.blast_radius == "MEDIUM"


def test_remove_netem_rules_is_low(catalog: ActionCatalog) -> None:
    entry = catalog.lookup("remove_netem_rules")
    assert entry is not None
    assert entry.blast_radius == "LOW"


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------


def test_all_remediation_actions_are_reversible(catalog: ActionCatalog) -> None:
    non_probe_ids = [
        "remove_scoped_db_reject",
        "kill_named_cpu_hog",
        "remove_named_memory_pressure_container",
        "remove_disk_fill_fixture",
        "restore_dns_alias",
        "remove_scoped_port_block",
        "remove_netem_rules",
        "restart_moodle_container",
        "restore_nginx_upstream",
        "restore_previous_approved_release",
        "remove_tagged_database_ingress_rule",
        "restore_moodledata_ownership",
        "restore_moodle_security_config",
    ]
    for action_id in non_probe_ids:
        entry = catalog.lookup(action_id)
        assert entry is not None, f"Action '{action_id}' not found in catalog"
        assert entry.is_reversible is True, f"Action '{action_id}' should be reversible"


# ---------------------------------------------------------------------------
# Adapter types
# ---------------------------------------------------------------------------


def test_valid_adapter_types(catalog: ActionCatalog) -> None:
    valid_adapters = {"docker", "ansible", "probe", "network"}
    for entry in catalog._entries.values():
        assert entry.adapter in valid_adapters, (
            f"Entry '{entry.action_id}' has unknown adapter='{entry.adapter}'"
        )

# ---------------------------------------------------------------------------
# ERPNext and full catalog
# ---------------------------------------------------------------------------


def test_erpnext_catalog_loads_actions() -> None:
    erpnext = ActionCatalog.load_erpnext_catalog()
    assert erpnext.lookup("start_erpnext_mariadb_container") is not None
    assert erpnext.lookup("start_erpnext_redis_container") is not None
    assert erpnext.lookup("restore_erpnext_nginx_config") is not None
    assert erpnext.is_allowed("start_erpnext_mariadb_container", environment="staging", role="executor") is True
    assert erpnext.is_allowed("start_erpnext_mariadb_container", environment="production", role="executor") is False


def test_full_catalog_includes_moodle_and_erpnext() -> None:
    full = ActionCatalog.load_full_catalog()
    assert full.lookup("remove_scoped_db_reject") is not None
    assert full.lookup("start_erpnext_mariadb_container") is not None
    assert len(full._entries) >= 20


def test_s3_3_execution_catalog_contains_only_reviewed_s3_2_pairs() -> None:
    catalog = ActionCatalog.load_moodle_s3_3_execution_catalog()
    assert set(catalog._entries) == {
        "remove_scoped_db_reject",
        "verify_tls_database_connection",
        "remove_named_cpu_load_container",
        "verify_node_cpu_recovers",
        "recreate_moodle_web_from_reviewed_compose",
        "verify_database_hostname",
        "start_reviewed_compose_service",
        "wait_for_alb_target_health",
        "restore_fixture_directory_mode",
        "verify_efs_write",
    }


def test_s3_3_execution_allows_only_exact_reviewed_tuple() -> None:
    catalog = ActionCatalog.load_moodle_s3_3_execution_catalog()
    assert catalog.is_safe_execution_allowed(
        "remove_scoped_db_reject",
        scenario_id="DB-01",
        target_scope="staging_moodle_nodes",
        environment="staging",
        role="executor",
    ) is True


@pytest.mark.parametrize(
    ("action_id", "scenario_id", "target_scope", "environment", "role"),
    [
        ("remove_scoped_db_reject", "DB-01", "staging_moodle_nodes", "production", "executor"),
        ("remove_scoped_db_reject", "DB-01", "moodle-app-b", "staging", "executor"),
        ("remove_scoped_db_reject", "SEC-02", "staging_moodle_nodes", "staging", "executor"),
        ("unrestricted_shell", "DB-01", "staging_moodle_nodes", "staging", "executor"),
        ("remove_scoped_db_reject", "DB-01", "staging_moodle_nodes", "staging", "observer"),
    ],
)
def test_s3_3_execution_rejects_outside_reviewed_boundary(
    action_id: str, scenario_id: str, target_scope: str, environment: str, role: str
) -> None:
    catalog = ActionCatalog.load_moodle_s3_3_execution_catalog()
    assert catalog.is_safe_execution_allowed(
        action_id,
        scenario_id=scenario_id,
        target_scope=target_scope,
        environment=environment,
        role=role,
    ) is False
