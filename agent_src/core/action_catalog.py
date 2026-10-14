"""Moodle action catalog with typed adapters and least-privilege permissions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from core.schema.common import ActionType, ResourceType

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
    action_type: ActionType | None = None
    description: str
    adapter: str  # "docker" | "ansible" | "probe" | "network"
    permission: ActionPermission
    blast_radius: str  # "LOW" | "MEDIUM" | "HIGH"
    is_reversible: bool
    rollback_action: str | None
    idempotent: bool
    forbidden_in_production: bool = True
    timeout_seconds: int = 120
    parameter_schema: dict[str, str] = Field(default_factory=dict)
    default_requires_approval: StrictBool | None = None
    valid_target_resource_types: list[ResourceType] = Field(default_factory=list)


_ACTION_TYPES: dict[str, ActionType] = {
    "remove_scoped_db_reject": ActionType.REMOVE_SCOPED_PORT_BLOCK,
    "kill_named_cpu_hog": ActionType.STOP_FAULT_INJECTOR,
    "remove_named_memory_pressure_container": ActionType.STOP_FAULT_INJECTOR,
    "remove_disk_fill_fixture": ActionType.STOP_FAULT_INJECTOR,
    "restore_dns_alias": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "remove_scoped_port_block": ActionType.REMOVE_SCOPED_PORT_BLOCK,
    "remove_netem_rules": ActionType.STOP_FAULT_INJECTOR,
    "restart_moodle_container": ActionType.RESTART_CONTAINER,
    "restore_nginx_upstream": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "restore_previous_approved_release": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "remove_tagged_database_ingress_rule": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "restore_moodledata_ownership": ActionType.RESTORE_MOODLEDATA_PERMISSION,
    "restore_moodle_security_config": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "verify_tls_database_connection": ActionType.READ_HEALTH,
    "probe_moodle_http_health": ActionType.READ_HEALTH,
    "probe_postgres_connectivity": ActionType.READ_HEALTH,
    "start_reviewed_compose_service": ActionType.START_CONTAINER,
    "wait_for_alb_target_health": ActionType.READ_HEALTH,
    "restore_moodle_apache_router": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "restore_approved_runtime_env": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "restore_database_connection_capacity": ActionType.STOP_FAULT_INJECTOR,
    "restore_approved_database_endpoint": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "recreate_moodle_web_from_reviewed_compose": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "verify_database_hostname": ActionType.READ_HEALTH,
    "remove_scoped_moodle_db_port_reject": ActionType.REMOVE_SCOPED_PORT_BLOCK,
    "remove_scoped_netem_profile": ActionType.STOP_FAULT_INJECTOR,
    "remove_named_cpu_load_container": ActionType.STOP_FAULT_INJECTOR,
    "verify_node_cpu_recovers": ActionType.READ_HEALTH,
    "remove_named_disk_fixture": ActionType.STOP_FAULT_INJECTOR,
    "restore_fixture_directory_mode": ActionType.RESTORE_MOODLEDATA_PERMISSION,
    "verify_efs_write": ActionType.READ_HEALTH,
    "restore_approved_proxy_configuration": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "probe_moodle_synthetic_login": ActionType.READ_HEALTH,
    "start_erpnext_mariadb_container": ActionType.START_CONTAINER,
    "start_erpnext_redis_container": ActionType.START_CONTAINER,
    "restore_erpnext_nginx_config": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "probe_erpnext_mariadb_connectivity": ActionType.READ_HEALTH,
    "probe_erpnext_redis_connectivity": ActionType.READ_HEALTH,
    "probe_erpnext_http_health": ActionType.READ_HEALTH,
}

_TARGET_RESOURCE_TYPES: dict[str, set[ResourceType]] = {
    "remove_scoped_db_reject": {ResourceType.APPLICATION, ResourceType.DATABASE},
    "kill_named_cpu_hog": {ResourceType.APPLICATION},
    "remove_named_memory_pressure_container": {ResourceType.APPLICATION},
    "remove_disk_fill_fixture": {ResourceType.APPLICATION},
    "restore_dns_alias": {ResourceType.APPLICATION, ResourceType.DATABASE},
    "remove_scoped_port_block": {ResourceType.DATABASE},
    "remove_netem_rules": {ResourceType.APPLICATION},
    "restart_moodle_container": {ResourceType.APPLICATION},
    "restore_nginx_upstream": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
    "restore_previous_approved_release": {ResourceType.APPLICATION},
    "remove_tagged_database_ingress_rule": {ResourceType.NETWORK},
    "restore_moodledata_ownership": {ResourceType.VOLUME},
    "restore_moodle_security_config": {ResourceType.APPLICATION},
    "verify_tls_database_connection": {ResourceType.DATABASE},
    "probe_moodle_http_health": {ResourceType.ENDPOINT, ResourceType.APPLICATION},
    "probe_postgres_connectivity": {ResourceType.DATABASE},
    "start_reviewed_compose_service": {ResourceType.APPLICATION},
    "wait_for_alb_target_health": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
    "restore_moodle_apache_router": {ResourceType.APPLICATION},
    "restore_approved_runtime_env": {ResourceType.APPLICATION},
    "restore_database_connection_capacity": {ResourceType.DATABASE},
    "restore_approved_database_endpoint": {ResourceType.DATABASE},
    "recreate_moodle_web_from_reviewed_compose": {ResourceType.APPLICATION},
    "verify_database_hostname": {ResourceType.DATABASE},
    "remove_scoped_moodle_db_port_reject": {ResourceType.DATABASE},
    "remove_scoped_netem_profile": {ResourceType.APPLICATION},
    "remove_named_cpu_load_container": {ResourceType.APPLICATION},
    "verify_node_cpu_recovers": {ResourceType.APPLICATION},
    "remove_named_disk_fixture": {ResourceType.APPLICATION},
    "restore_fixture_directory_mode": {ResourceType.VOLUME},
    "verify_efs_write": {ResourceType.VOLUME},
    "restore_approved_proxy_configuration": {ResourceType.APPLICATION},
    "probe_moodle_synthetic_login": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
    "start_erpnext_mariadb_container": {ResourceType.DATABASE, ResourceType.CONTAINER},
    "start_erpnext_redis_container": {ResourceType.CONTAINER},
    "restore_erpnext_nginx_config": {ResourceType.APPLICATION},
    "probe_erpnext_mariadb_connectivity": {ResourceType.DATABASE},
    "probe_erpnext_redis_connectivity": {ResourceType.CONTAINER},
    "probe_erpnext_http_health": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
}


# The only scenario/action/target tuples permitted for the first live
# execution slice.  Values are logical target scopes from the Moodle capability
# contract, never host names or shell commands.
_S3_3_STAGING_EXECUTION_SCOPES: dict[str, dict[str, str]] = {
    "DB-01": {
        "remove_scoped_db_reject": "staging_moodle_nodes",
        "verify_tls_database_connection": "staging_moodle_nodes",
    },
    "RES-01": {
        "remove_named_cpu_load_container": "moodle-app-b",
        "verify_node_cpu_recovers": "moodle-app-b",
    },
    "NET-01": {
        "recreate_moodle_web_from_reviewed_compose": "staging_moodle_nodes",
        "verify_database_hostname": "staging_moodle_nodes",
    },
    "CON-01": {
        "start_reviewed_compose_service": "moodle-app-b",
        "wait_for_alb_target_health": "moodle-app-b",
    },
    "SEC-02": {
        "restore_fixture_directory_mode": "moodledata_synthetic_fixture_directory",
        "verify_efs_write": "moodle-synthetic-transaction",
    },
}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class ActionCatalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        for entry in entries:
            if entry.action_id in self._entries:
                raise ValueError(f"duplicate action_id in catalog: {entry.action_id}")
            action_type = entry.action_type or _ACTION_TYPES.get(entry.action_id)
            target_types = entry.valid_target_resource_types or sorted(
                _TARGET_RESOURCE_TYPES.get(entry.action_id, set()), key=lambda item: item.value
            )
            if action_type is not None or target_types:
                default_approval = (
                    entry.permission.required_role == "operator"
                    or entry.blast_radius in {"MEDIUM", "HIGH"}
                )
                entry = entry.model_copy(update={
                    "action_type": action_type or entry.action_type,
                    "valid_target_resource_types": target_types,
                    "default_requires_approval": (
                        entry.default_requires_approval
                        if entry.default_requires_approval is not None
                        else default_approval
                    ),
                })
            self._entries[entry.action_id] = entry

    def lookup(self, action_id: str) -> CatalogEntry | None:
        return self._entries.get(action_id)

    def target_type_allowed(self, action_id: str, target_type: ResourceType) -> bool:
        entry = self.lookup(action_id)
        return bool(entry and target_type in entry.valid_target_resource_types)

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

    def is_safe_execution_allowed(
        self, action_id: str, *, scenario_id: str, target_scope: str, environment: str, role: str
    ) -> bool:
        """Fail closed unless a request matches the reviewed S3.3 live boundary."""
        scope = _S3_3_STAGING_EXECUTION_SCOPES.get(scenario_id)
        if scope is None or environment != "staging" or scope.get(action_id) != target_scope:
            return False
        return self.is_allowed(action_id, environment=environment, role=role)

    # ------------------------------------------------------------------

    @classmethod
    def load_moodle_catalog(cls) -> ActionCatalog:
        """Return a catalog pre-loaded with all Moodle scenario actions."""
        _executor_staging = ActionPermission(
            required_role="executor",
            environment_scope=["staging"],
        )
        _operator_staging = ActionPermission(
            required_role="operator",
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
                description="Human-only removal of the exact tagged unauthorized RDS security-group ingress rule.",
                adapter="ansible",
                permission=_operator_staging,
                blast_radius="HIGH",
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
                action_id="restore_moodle_apache_router",
                description="Recreate only moodle-app-b web from the approved image to restore its Apache router configuration.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="restore_approved_runtime_env",
                description="Restore moodle-app-b's exact pre-fault runtime env and recreate its unchanged approved image.",
                adapter="ansible",
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
                description="Remove only the named file from moodle-app-b's isolated scratch filesystem; never fill EFS.",
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
    def load_moodle_s3_3_execution_catalog(cls) -> ActionCatalog:
        """Load only the reviewed remediation/probe pairs from the S3.2 campaign."""
        full_catalog = cls.load_moodle_catalog()
        action_ids = {
            action_id
            for actions in _S3_3_STAGING_EXECUTION_SCOPES.values()
            for action_id in actions
        }
        return cls(entries=[full_catalog._entries[action_id] for action_id in sorted(action_ids)])

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
