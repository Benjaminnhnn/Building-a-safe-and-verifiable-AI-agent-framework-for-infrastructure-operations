"""Moodle action catalog with typed adapters and least-privilege permissions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from core.environment_profile import get_environment_profile
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
    "reconcile_db01_safe_baseline": ActionType.REMOVE_SCOPED_PORT_BLOCK,
    "restore_dns_alias": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "restart_moodle_container": ActionType.RESTART_CONTAINER,
    "remove_tagged_database_ingress_rule": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "verify_tls_database_connection": ActionType.READ_HEALTH,
    "probe_moodle_http_health": ActionType.READ_HEALTH,
    "probe_postgres_connectivity": ActionType.READ_HEALTH,
    "start_reviewed_compose_service": ActionType.START_CONTAINER,
    "reconcile_con01_safe_baseline": ActionType.START_CONTAINER,
    "wait_for_alb_target_health": ActionType.READ_HEALTH,
    "recreate_moodle_web_from_reviewed_compose": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "reconcile_net01_safe_baseline": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "verify_database_hostname": ActionType.READ_HEALTH,
    "remove_named_cpu_load_container": ActionType.STOP_FAULT_INJECTOR,
    "reconcile_res01_safe_baseline": ActionType.STOP_FAULT_INJECTOR,
    "verify_node_cpu_recovers": ActionType.READ_HEALTH,
    "probe_moodle_synthetic_login": ActionType.READ_HEALTH,
    "start_erpnext_mariadb_container": ActionType.START_CONTAINER,
    "stop_erpnext_mariadb_container": ActionType.START_CONTAINER,
    "start_erpnext_redis_container": ActionType.START_CONTAINER,
    "stop_erpnext_redis_container": ActionType.START_CONTAINER,
    "restore_erpnext_nginx_config": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "reconcile_erpnext_nginx_safe_baseline": ActionType.RUN_ANSIBLE_PLAYBOOK,
    "probe_erpnext_mariadb_connectivity": ActionType.READ_HEALTH,
    "probe_erpnext_redis_connectivity": ActionType.READ_HEALTH,
    "probe_erpnext_http_health": ActionType.READ_HEALTH,
    # Business / Infrastructure workflow actions (BIZ-01, BIZ-02)
    "disable_moodle_cli_maintenance_mode": ActionType.RECONFIGURE_SERVICE,
    "enable_moodle_cli_maintenance_mode": ActionType.RECONFIGURE_SERVICE,
    "verify_student_course_access": ActionType.READ_HEALTH,
    "restart_moodle_cron_service": ActionType.START_CONTAINER,
    "reconcile_biz02_cron_baseline": ActionType.START_CONTAINER,
    "verify_moodle_cron_running": ActionType.READ_HEALTH,
}

_TARGET_RESOURCE_TYPES: dict[str, set[ResourceType]] = {
    "remove_scoped_db_reject": {ResourceType.APPLICATION, ResourceType.DATABASE},
    "reconcile_db01_safe_baseline": {ResourceType.APPLICATION, ResourceType.DATABASE},
    "restore_dns_alias": {ResourceType.APPLICATION, ResourceType.DATABASE},
    "restart_moodle_container": {ResourceType.APPLICATION},
    "remove_tagged_database_ingress_rule": {ResourceType.NETWORK},
    "verify_tls_database_connection": {ResourceType.DATABASE},
    "probe_moodle_http_health": {ResourceType.ENDPOINT, ResourceType.APPLICATION},
    "probe_postgres_connectivity": {ResourceType.DATABASE},
    "start_reviewed_compose_service": {ResourceType.APPLICATION},
    "reconcile_con01_safe_baseline": {ResourceType.APPLICATION},
    "wait_for_alb_target_health": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
    "recreate_moodle_web_from_reviewed_compose": {ResourceType.APPLICATION},
    "reconcile_net01_safe_baseline": {ResourceType.APPLICATION},
    "verify_database_hostname": {ResourceType.DATABASE},
    "remove_named_cpu_load_container": {ResourceType.APPLICATION},
    "reconcile_res01_safe_baseline": {ResourceType.APPLICATION},
    "verify_node_cpu_recovers": {ResourceType.APPLICATION},
    "probe_moodle_synthetic_login": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
    "start_erpnext_mariadb_container": {ResourceType.DATABASE, ResourceType.CONTAINER},
    "stop_erpnext_mariadb_container": {ResourceType.DATABASE, ResourceType.CONTAINER},
    "start_erpnext_redis_container": {ResourceType.CONTAINER},
    "stop_erpnext_redis_container": {ResourceType.CONTAINER},
    "restore_erpnext_nginx_config": {ResourceType.APPLICATION},
    "reconcile_erpnext_nginx_safe_baseline": {ResourceType.APPLICATION},
    "probe_erpnext_mariadb_connectivity": {ResourceType.DATABASE},
    "probe_erpnext_redis_connectivity": {ResourceType.CONTAINER},
    "probe_erpnext_http_health": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
    # Business / Infrastructure workflow target resource types (BIZ-01, BIZ-02)
    "disable_moodle_cli_maintenance_mode": {ResourceType.APPLICATION},
    "enable_moodle_cli_maintenance_mode": {ResourceType.APPLICATION},
    "verify_student_course_access": {ResourceType.APPLICATION, ResourceType.ENDPOINT},
    "restart_moodle_cron_service": {ResourceType.APPLICATION, ResourceType.CONTAINER},
    "reconcile_biz02_cron_baseline": {ResourceType.APPLICATION, ResourceType.CONTAINER},
    "verify_moodle_cron_running": {ResourceType.APPLICATION, ResourceType.CONTAINER},
}


# The only scenario/action/target tuples permitted for the first live
# execution slice.  Values are logical target scopes from the Moodle capability
# contract, never host names or shell commands.
_S3_3_STAGING_EXECUTION_SCOPES: dict[str, dict[str, str]] = {
    "DB-01": {
        "remove_scoped_db_reject": "staging_moodle_nodes",
        "reconcile_db01_safe_baseline": "staging_moodle_nodes",
        "verify_tls_database_connection": "staging_moodle_nodes",
    },
    "RES-01": {
        "remove_named_cpu_load_container": "moodle-app-b",
        "reconcile_res01_safe_baseline": "moodle-app-b",
        "verify_node_cpu_recovers": "moodle-app-b",
    },
    "NET-01": {
        "recreate_moodle_web_from_reviewed_compose": "staging_moodle_nodes",
        "reconcile_net01_safe_baseline": "staging_moodle_nodes",
        "verify_database_hostname": "staging_moodle_nodes",
    },
    "CON-01": {
        "start_reviewed_compose_service": "moodle-app-b",
        "reconcile_con01_safe_baseline": "moodle-app-b",
        "wait_for_alb_target_health": "moodle-app-b",
    },
}

# Business / Infrastructure workflow scenarios permitted for staging execution.
_BIZ_STAGING_EXECUTION_SCOPES: dict[str, dict[str, str]] = {
    "BIZ-01": {
        "disable_moodle_cli_maintenance_mode": "staging_moodle_app",
        "enable_moodle_cli_maintenance_mode": "staging_moodle_app",
        "verify_student_course_access": "staging_moodle_app",
    },
    "BIZ-02": {
        "restart_moodle_cron_service": "staging_moodle_app",
        "reconcile_biz02_cron_baseline": "staging_moodle_app",
        "verify_moodle_cron_running": "staging_moodle_app",
    },
}

# Canonical 7-scenario suite: 2 Business/Infra (BIZ-01, BIZ-02) + 5 Technical (CON-01, DB-01, RES-01, NET-01, SEC-01)
MOODLE_7_SCENARIO_IDS: tuple[str, ...] = (
    "BIZ-01",
    "BIZ-02",
    "CON-01",
    "DB-01",
    "RES-01",
    "NET-01",
    "SEC-01",
)


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
        """Fail closed unless a request matches the reviewed S3.3 or Business live boundary."""
        scope = _S3_3_STAGING_EXECUTION_SCOPES.get(scenario_id) or _BIZ_STAGING_EXECUTION_SCOPES.get(scenario_id)
        # The environment must be the one the reviewed profile describes AND be
        # listed in the entry's environment_scope (checked by is_allowed). No
        # production entry exists until one is reviewed, so production denies.
        if (
            scope is None
            or environment != get_environment_profile().environment
            or scope.get(action_id) != target_scope
        ):
            return False
        return self.is_allowed(action_id, environment=environment, role=role)

    # ------------------------------------------------------------------

    @classmethod
    def load_moodle_catalog(cls) -> ActionCatalog:
        """Return the catalog for the seven thesis Moodle scenarios."""
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
                rollback_action="reconcile_db01_safe_baseline",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="reconcile_db01_safe_baseline",
                description="Re-apply the reviewed DB-01 safe baseline after partial execution.",
                action_type=ActionType.REMOVE_SCOPED_PORT_BLOCK,
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="remove_scoped_db_reject",
                idempotent=True,
                forbidden_in_production=True,
                valid_target_resource_types=[ResourceType.APPLICATION, ResourceType.DATABASE],
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
                action_id="remove_tagged_database_ingress_rule",
                description="Human-only removal of the exact tagged unauthorized RDS security-group ingress rule.",
                adapter="ansible",
                permission=_operator_staging,
                blast_radius="HIGH",
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
            # Reviewed staging remediation actions and probes
            # ---------------------------------------------------------------
            CatalogEntry(
                action_id="start_reviewed_compose_service",
                description="Start stopped Moodle web container from reviewed compose file.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="reconcile_con01_safe_baseline",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="reconcile_con01_safe_baseline",
                description="Re-apply the reviewed CON-01 safe baseline after partial execution.",
                action_type=ActionType.START_CONTAINER,
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="start_reviewed_compose_service",
                idempotent=True,
                forbidden_in_production=True,
                valid_target_resource_types=[ResourceType.APPLICATION],
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
                action_id="recreate_moodle_web_from_reviewed_compose",
                description="Recreate Moodle web container from reviewed compose manifest.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="MEDIUM",
                is_reversible=True,
                rollback_action="reconcile_net01_safe_baseline",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="reconcile_net01_safe_baseline",
                description="Re-apply the reviewed NET-01 safe baseline after partial execution.",
                action_type=ActionType.RUN_ANSIBLE_PLAYBOOK,
                adapter="docker",
                permission=_executor_staging,
                blast_radius="MEDIUM",
                is_reversible=True,
                rollback_action="recreate_moodle_web_from_reviewed_compose",
                idempotent=True,
                forbidden_in_production=True,
                valid_target_resource_types=[ResourceType.APPLICATION],
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
                action_id="remove_named_cpu_load_container",
                description="Stop and remove CPU load fixture container.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="reconcile_res01_safe_baseline",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="reconcile_res01_safe_baseline",
                description="Re-apply the reviewed RES-01 safe baseline after partial execution.",
                action_type=ActionType.STOP_FAULT_INJECTOR,
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="remove_named_cpu_load_container",
                idempotent=True,
                forbidden_in_production=True,
                valid_target_resource_types=[ResourceType.APPLICATION],
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

        # The Business actions (BIZ-01, BIZ-02) live in their own catalog so the
        # reviewed live slice can be audited separately; the Moodle catalog
        # covers all seven thesis scenarios.
        entries.extend(cls.load_moodle_business_catalog()._entries.values())
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
    def load_moodle_business_catalog(cls) -> ActionCatalog:
        """Return a catalog pre-loaded with the 5 Moodle Business/Admin scenarios (BIZ-01..BIZ-05)."""
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
            # BIZ-01: Maintenance Mode Stuck
            CatalogEntry(
                action_id="disable_moodle_cli_maintenance_mode",
                description="Disable stuck Moodle CLI maintenance mode (php admin/cli/maintenance.php --disable).",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="enable_moodle_cli_maintenance_mode",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="enable_moodle_cli_maintenance_mode",
                description="Re-enable Moodle CLI maintenance mode if rollback is required.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="disable_moodle_cli_maintenance_mode",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="verify_student_course_access",
                description="Probe non-admin student course view to confirm maintenance mode is cleared.",
                adapter="probe",
                permission=_verifier_staging_ro,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
            # BIZ-02: Ad-hoc / Scheduled Task Queue Stuck (Infrastructure Level: Restart moodle-cron)
            CatalogEntry(
                action_id="restart_moodle_cron_service",
                description="Restart moodle-cron container on node A when ad-hoc task queue is overdue; does not mutate Moodle DB tables.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="reconcile_biz02_cron_baseline",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="reconcile_biz02_cron_baseline",
                description="Ensure moodle-cron container is running at baseline configuration after restart attempt.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="restart_moodle_cron_service",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="verify_moodle_cron_running",
                description="Probe moodle-cron container state and verify task queue processing recovers.",
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
    def load_moodle_7_scenario_catalog(cls) -> ActionCatalog:
        """Load the focused 7-scenario catalog: 5 Technical (CON-01, DB-01, RES-01, NET-01, SEC-01) + 2 Business/Infra (BIZ-01, BIZ-02) and S3.3 slice."""
        s3_3 = cls.load_moodle_s3_3_execution_catalog()
        moodle_full = cls.load_moodle_catalog()
        tech_entries = [moodle_full._entries["remove_tagged_database_ingress_rule"]]
        biz_entries = [moodle_full._entries[action_id] for action_id in cls.load_moodle_business_catalog()._entries]
        return cls(entries=list(s3_3._entries.values()) + tech_entries + biz_entries)

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
                action_id="stop_erpnext_mariadb_container",
                description="Re-apply ERPNext MariaDB safe baseline after partial execution.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="start_erpnext_mariadb_container",
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
                action_id="stop_erpnext_redis_container",
                description="Re-apply ERPNext Redis safe baseline after partial execution.",
                adapter="docker",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="start_erpnext_redis_container",
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
                rollback_action="reconcile_erpnext_nginx_safe_baseline",
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="reconcile_erpnext_nginx_safe_baseline",
                description="Re-apply ERPNext Nginx safe baseline after restore_erpnext_nginx_config.",
                adapter="ansible",
                permission=_executor_staging,
                blast_radius="LOW",
                is_reversible=True,
                rollback_action="restore_erpnext_nginx_config",
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
    def load_reviewed_rollback_catalog(cls) -> ActionCatalog:
        """Catalog used to look up reciprocal rollback actions (Moodle and ERPNext)."""
        return cls.load_full_catalog()

    @classmethod
    def load_full_catalog(cls) -> ActionCatalog:
        """Return a combined catalog covering both Moodle and ERPNext actions."""
        moodle = cls.load_moodle_catalog()
        erpnext = cls.load_erpnext_catalog()
        combined = list(moodle._entries.values()) + list(erpnext._entries.values())
        return cls(entries=combined)
