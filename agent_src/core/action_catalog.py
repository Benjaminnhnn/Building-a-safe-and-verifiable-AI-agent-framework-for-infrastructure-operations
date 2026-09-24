"""Moodle action catalog with typed adapters and least-privilege permissions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ActionPermission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_role: str
    environment_scope: list[str]
    read_only: bool = False


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    description: str
    adapter: str  # "docker" | "ansible" | "probe" | "network"
    permission: ActionPermission
    blast_radius: str  # "LOW" | "MEDIUM" | "HIGH"
    is_reversible: bool
    rollback_action: str | None
    idempotent: bool
    forbidden_in_production: bool = True
    timeout_seconds: int = 120


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class ActionCatalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries: dict[str, CatalogEntry] = {e.action_id: e for e in entries}

    def lookup(self, action_id: str) -> CatalogEntry | None:
        return self._entries.get(action_id)

    def is_allowed(self, action_id: str, *, environment: str, role: str) -> bool:
        entry = self.lookup(action_id)
        if entry is None:
            return False
        perm = entry.permission
        if environment not in perm.environment_scope:
            return False
        if environment == "production" and entry.forbidden_in_production:
            return False
        # Verifier can use read-only probes
        if perm.read_only and role == "verifier":
            return True
        return role == perm.required_role

    # ------------------------------------------------------------------

    @classmethod
    def load_moodle_catalog(cls) -> ActionCatalog:
        """Return a catalog pre-loaded with all Moodle scenario actions."""
        _executor_staging = ActionPermission(
            required_role="executor",
            environment_scope=["staging"],
        )
        _verifier_staging_ro = ActionPermission(
            required_role="verifier",
            environment_scope=["staging"],
            read_only=True,
        )

        entries: list[CatalogEntry] = [
            # ---------------------------------------------------------------
            # Remediation actions
            # ---------------------------------------------------------------
            CatalogEntry(
                action_id="remove_scoped_db_reject",
                description="Remove scoped iptables rule blocking DB connections.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="restore_scoped_db_reject",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="kill_named_cpu_hog",
                description="Kill a named container causing CPU exhaustion.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=False,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="remove_named_memory_pressure_container",
                description="Stop and remove the named memory-pressure fixture container.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="remove_disk_fill_fixture",
                description="Remove disk-fill fixture file to reclaim disk space.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_dns_alias",
                description="Restore the Moodle DNS alias to the correct upstream.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="remove_scoped_port_block",
                description="Remove scoped iptables rule blocking a specific port.",
                adapter="network",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="restore_scoped_port_block",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="remove_netem_rules",
                description="Remove tc netem latency/loss rules from the network interface.",
                adapter="network",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restart_moodle_container",
                description="Restart the Moodle application container.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="MEDIUM",
                is_reversible=True,
                rollback_action=None,
                idempotent=False,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_nginx_upstream",
                description="Restore the Nginx upstream configuration to the correct target.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_previous_approved_release",
                description="Roll back to the previously approved Moodle release image.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="MEDIUM",
                is_reversible=True,
                rollback_action=None,
                idempotent=False,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="remove_tagged_database_ingress_rule",
                description="Remove a tagged firewall rule that exposes the database port.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="restore_tagged_database_ingress_rule",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_moodledata_ownership",
                description="Restore correct ownership and permissions on moodledata volume.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_moodle_security_config",
                description="Restore trusted proxy/wwwroot/security settings in Moodle config.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            # ---------------------------------------------------------------
            # Read-only probes (verifier)
            # ---------------------------------------------------------------
            CatalogEntry(
                action_id="verify_tls_database_connection",
                description="Probe TLS connectivity from Moodle to the database.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="probe_moodle_http_health",
                description="HTTP health-check probe against the Moodle frontend.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="probe_postgres_connectivity",
                description="Test TCP connectivity to the PostgreSQL port.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),

            # ---------------------------------------------------------------
            # Additional Moodle 15-scenario remediation actions and probes
            # ---------------------------------------------------------------
            CatalogEntry(
                action_id="start_reviewed_compose_service",
                description="Start stopped Moodle web container from reviewed compose file.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="stop_reviewed_compose_service",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="wait_for_alb_target_health",
                description="Wait for ALB target health check to report target healthy.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="start_moodle_reverse_proxy",
                description="Start or reload the Moodle reverse proxy service.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_database_connection_capacity",
                description="Terminate runaway connection holder and restore pool capacity.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_approved_database_endpoint",
                description="Restore reviewed database endpoint configuration.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="recreate_moodle_web_from_reviewed_compose",
                description="Recreate Moodle web container from reviewed compose manifest.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="MEDIUM",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="verify_database_hostname",
                description="Probe database hostname resolution and connectivity.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="remove_scoped_moodle_db_port_reject",
                description="Remove scoped port reject rule on DB port 5432.",
                adapter="network",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="remove_scoped_netem_profile",
                description="Remove tc netem packet delay/loss profile.",
                adapter="network",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="remove_named_cpu_load_container",
                description="Stop and remove CPU load fixture container.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="verify_node_cpu_recovers",
                description="Probe node CPU metrics to verify recovery.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="remove_named_disk_fixture",
                description="Delete named disk fill fixture file.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_fixture_directory_mode",
                description="Restore correct directory permissions on moodledata.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="verify_efs_write",
                description="Test write access to the EFS moodledata mount.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="restore_approved_proxy_configuration",
                description="Restore approved reverse proxy configuration from baseline.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="probe_moodle_synthetic_login",
                description="Run a synthetic login transaction against Moodle.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
        ]

        return cls(entries=entries)

    @classmethod
    def load_erpnext_catalog(cls) -> ActionCatalog:
        """Return a catalog pre-loaded with all ERPNext generalization actions."""
        _executor_staging = ActionPermission(
            required_role="executor",
            environment_scope=["staging"],
        )
        _verifier_staging_ro = ActionPermission(
            required_role="verifier",
            environment_scope=["staging"],
            read_only=True,
        )
        entries: list[CatalogEntry] = [
            CatalogEntry(
                action_id="start_erpnext_mariadb_container",
                description="Start stopped MariaDB container in ERPNext Compose stack.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="stop_erpnext_mariadb_container",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="start_erpnext_redis_container",
                description="Start stopped Redis container in ERPNext Compose stack.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="stop_erpnext_redis_container",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_erpnext_nginx_config",
                description="Restore ERPNext Nginx configuration from recorded baseline.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="probe_erpnext_mariadb_connectivity",
                description="Test TCP connectivity to MariaDB port in ERPNext stack.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="probe_erpnext_redis_connectivity",
                description="Test TCP connectivity to Redis port in ERPNext stack.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            CatalogEntry(
                action_id="probe_erpnext_http_health",
                description="HTTP health-check probe against ERPNext web frontend.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
        ]
        return cls(entries=entries)

    @classmethod
    def load_full_catalog(cls) -> ActionCatalog:
        """Return a combined catalog covering both Moodle and ERPNext actions."""
        moodle = cls.load_moodle_catalog()
        erpnext = cls.load_erpnext_catalog()
        combined = list(moodle._entries.values()) + list(erpnext._entries.values())
        return cls(entries=combined)
