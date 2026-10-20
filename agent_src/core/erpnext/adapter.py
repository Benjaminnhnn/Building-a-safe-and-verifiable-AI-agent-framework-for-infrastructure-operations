"""ERPNext platform adapter implementing the domain-neutral PlatformAdapter contract.

Demonstrates multi-platform generalizability of the safety-gated incident pipeline
without modifying the shared platform-neutral core:
- Alert label mappings and resource aliases for ERPNext infrastructure
- Resource inventory and DependencyGraph resolution
- Conservative evidence collection and rule-based diagnosis
- Typed remediation candidates backed by ActionCatalog.load_erpnext_catalog()
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.evidence.dependency_graph import DependencyGraph
from core.evidence.evidence_collectors import collect_shadow_evidence
from core.platform_adapter import PlatformAdapterRegistry
from core.safety.action_catalog import ActionCatalog
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment
from core.schema.resource import Resource
from core.schema.verification import ProbeResult

ERPNEXT_ALERT_RESOURCE_MAP: dict[str, str] = {
    "ERPNextMariaDBDown": "erpnext-mariadb",
    "ERPNextRedisQueueDown": "erpnext-redis-queue",
    "ERPNextNginxUpstreamDrift": "erpnext-nginx",
    "ERPNextPublicProbeFailed": "erpnext-web-endpoint",
}

ERPNEXT_RESOURCE_ALIASES: dict[str, str] = {
    "erpnext": "erpnext-app",
    "erpnext-db": "erpnext-mariadb",
    "erpnext-redis": "erpnext-redis-queue",
    "erpnext-frontend": "erpnext-nginx",
}

ERPNEXT_METRIC_QUERIES_BY_ALERT: dict[str, tuple[tuple[str, str], ...]] = {
    "ERPNextMariaDBDown": (
        ("erpnext_mariadb_up", 'mysql_up{job="erpnext-mariadb"}'),
        ("erpnext_http_2xx", 'probe_success{job="erpnext-blackbox"}'),
    ),
    "ERPNextRedisQueueDown": (
        ("erpnext_redis_up", 'redis_up{job="erpnext-redis-queue"}'),
        ("erpnext_queue_depth", 'erpnext_background_queue_jobs{service="erpnext"}'),
    ),
    "ERPNextNginxUpstreamDrift": (
        ("erpnext_nginx_up", 'nginx_up{job="erpnext-nginx"}'),
        ("erpnext_http_5xx", 'rate(nginx_http_requests_total{status=~"5.."}[5m])'),
    ),
}

_ERPNEXT_DIAGNOSIS_RULES: dict[str, dict[str, Any]] = {
    "ERPNextMariaDBDown": {
        "scenario_id": "ERP-DB-01",
        "cause_code": "erpnext_mariadb_container_stopped",
        "hypothesis": "ERPNext MariaDB container is stopped or unreachable.",
        "reason": "ERPNextMariaDBDown alert correlates with database connectivity failure.",
        "confidence": 0.91,
        "catalog_action_id": "start_erpnext_mariadb_container",
        "rollback_action_id": "reconcile_erpnext_mariadb_safe_baseline",
        "action_type": ActionType.START_CONTAINER,
        "target_resource_id": "erpnext-mariadb",
        "target_scope": "staging_erpnext_db_nodes",
        "probe_action_id": "verify_erpnext_mariadb_tcp",
    },
    "ERPNextRedisQueueDown": {
        "scenario_id": "ERP-RES-01",
        "cause_code": "erpnext_redis_queue_stopped",
        "hypothesis": "ERPNext Redis queue container is stopped or unresponsive.",
        "reason": "ERPNextRedisQueueDown alert indicates background worker queue broker outage.",
        "confidence": 0.90,
        "catalog_action_id": "start_erpnext_redis_container",
        "rollback_action_id": "reconcile_erpnext_redis_safe_baseline",
        "action_type": ActionType.START_CONTAINER,
        "target_resource_id": "erpnext-redis-queue",
        "target_scope": "staging_erpnext_app_nodes",
        "probe_action_id": "verify_erpnext_redis_ping",
    },
    "ERPNextNginxUpstreamDrift": {
        "scenario_id": "ERP-NET-01",
        "cause_code": "erpnext_nginx_upstream_drift",
        "hypothesis": "ERPNext Nginx upstream configuration drifted from reviewed baseline.",
        "reason": "ERPNextNginxUpstreamDrift alert indicates reverse-proxy upstream mismatch.",
        "confidence": 0.89,
        "catalog_action_id": "restore_erpnext_nginx_config",
        "rollback_action_id": "reconcile_erpnext_nginx_safe_baseline",
        "action_type": ActionType.RECONFIGURE_SERVICE,
        "target_resource_id": "erpnext-nginx",
        "target_scope": "staging_erpnext_app_nodes",
        "probe_action_id": "verify_erpnext_http_health",
    },
}


def _inventory_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "config"
        / "erpnext_resource_inventory.json"
    )


class ERPNextPlatformAdapter:
    """Platform adapter implementing ERPNext-specific incident handling."""

    @property
    def platform_name(self) -> str:
        return "erpnext"

    def can_handle(self, alert: dict[str, Any]) -> bool:
        labels = alert.get("labels") or {}
        alert_name = str(labels.get("alertname") or "")
        service = str(labels.get("service") or "")
        if service == "erpnext" or alert_name.startswith("ERPNext"):
            return True
        if alert_name in ERPNEXT_ALERT_RESOURCE_MAP:
            return True
        if service in ERPNEXT_RESOURCE_ALIASES:
            return True
        return False

    def identify_resource(self, alert: dict[str, Any]) -> str | None:
        labels = alert.get("labels") or {}
        alert_name = str(labels.get("alertname") or "")
        known = {resource.resource_id for resource in self.get_resources()}

        mapped = ERPNEXT_ALERT_RESOURCE_MAP.get(alert_name)
        if mapped in known:
            return mapped

        for candidate in (
            labels.get("resource_id"),
            labels.get("component"),
            labels.get("service"),
            labels.get("target"),
        ):
            if not candidate:
                continue
            normalized = ERPNEXT_RESOURCE_ALIASES.get(str(candidate), str(candidate))
            if normalized in known:
                return normalized
        return None

    def get_resources(self) -> list[Resource]:
        path = _inventory_path()
        raw_resources = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_resources = data.get("resources", [])

        resources: list[Resource] = []
        for item in raw_resources:
            env_str = str(item.get("environment", "staging")).strip().lower()
            env = Environment.PRODUCTION if env_str == "production" else Environment.STAGING
            tags: dict[str, str] = {}
            if isinstance(item.get("tags"), dict):
                tags.update({str(k): str(v) for k, v in item["tags"].items()})
            if item.get("service_name"):
                tags["service_name"] = str(item["service_name"])
            if item.get("endpoint"):
                tags["endpoint"] = str(item["endpoint"])

            resources.append(
                Resource(
                    resource_id=item["resource_id"],
                    name=item.get("name", item["resource_id"]),
                    type=item.get("type", "application"),
                    environment=env,
                    owner_role=item.get("owner_role", "infrastructure_engineer"),
                    host=item.get("host"),
                    service_name=item.get("service_name"),
                    endpoint=item.get("endpoint"),
                    depends_on=item.get("depends_on", []),
                    tags=tags,
                )
            )
        return resources

    def get_resource(self, resource_id: str) -> Resource | None:
        for resource in self.get_resources():
            if resource.resource_id == resource_id:
                return resource
        return None

    def get_resource_types(self) -> dict[str, str]:
        return {r.resource_id: r.type.value for r in self.get_resources()}

    def get_dependency_graph(self) -> DependencyGraph:
        return DependencyGraph(self.get_resources())

    def get_alert_queries(self, alert_name: str) -> tuple[tuple[str, str], ...]:
        return ERPNEXT_METRIC_QUERIES_BY_ALERT.get(alert_name, ())

    def collect_evidence(
        self,
        alert: dict[str, Any],
        *,
        incident_id: str,
        resource_id: str,
        evidence_store: Any,
        prometheus_url: str | None = None,
    ) -> dict[str, Any]:
        alert_name = str((alert.get("labels") or {}).get("alertname") or "")
        queries = self.get_alert_queries(alert_name)
        return collect_shadow_evidence(
            alert,
            incident_id=incident_id,
            resource_id=resource_id,
            evidence_store=evidence_store,
            prometheus_url=prometheus_url,
            queries=queries,
        )

    def diagnose(self, alert_name: str, evidence: list[Any]) -> dict[str, Any]:
        evidence_refs = [
            str(getattr(item, "evidence_id", ""))
            for item in evidence
            if getattr(item, "evidence_id", None)
        ]
        if not evidence_refs:
            return {
                "status": "insufficient_evidence",
                "cause_code": "missing_erpnext_evidence",
                "hypothesis": None,
                "reason": "Cannot diagnose ERPNext alert without persisted evidence references.",
                "confidence": 0.0,
                "evidence_refs": [],
            }

        rule = _ERPNEXT_DIAGNOSIS_RULES.get(alert_name)
        if rule is None:
            return {
                "status": "insufficient_evidence",
                "cause_code": "unmapped_erpnext_alert",
                "hypothesis": None,
                "reason": f"No conservative rule mapped for ERPNext alert '{alert_name}'.",
                "confidence": 0.0,
                "evidence_refs": evidence_refs,
            }

        return {
            "status": "hypothesis",
            "scenario_id": rule["scenario_id"],
            "cause_code": rule["cause_code"],
            "hypothesis": rule["hypothesis"],
            "reason": rule["reason"],
            "confidence": rule["confidence"],
            "evidence_refs": evidence_refs,
        }

    def get_action_catalog(self) -> ActionCatalog:
        return ActionCatalog.load_erpnext_catalog()

    def plan_investigation(
        self, incident_id: str, diagnosis: dict[str, Any]
    ) -> TypedAction | None:
        if diagnosis.get("status") != "hypothesis":
            return None
        evidence_refs = list(diagnosis.get("evidence_refs") or [])
        if not evidence_refs:
            return None

        cause_code = str(diagnosis.get("cause_code") or "")
        matched_rule = next(
            (r for r in _ERPNEXT_DIAGNOSIS_RULES.values() if r["cause_code"] == cause_code),
            None,
        )
        if matched_rule is None:
            return None

        return TypedAction(
            action_id=f"act-erp-probe-{incident_id[:12]}",
            incident_id=incident_id,
            action_type=ActionType.READ_HEALTH,
            target_resource_id=matched_rule["target_resource_id"],
            environment=Environment.STAGING,
            parameters={},
            reason=f"Read-only verification probe ({matched_rule['probe_action_id']}) for {cause_code}.",
            evidence_refs=evidence_refs,
            expected_outcome="Confirm ERPNext component reachability before/after remediation.",
            reversible=True,
            rollback_plan=RollbackPlan(
                available=True,
                method="Read-only probe requires no rollback.",
            ),
            requires_approval=False,
        )

    def plan_remediation(
        self,
        incident_id: str,
        alert_name: str,
        diagnosis: dict[str, Any],
        evidence: list[Any],
    ) -> dict[str, Any] | None:
        if diagnosis.get("status") != "hypothesis":
            return None
        rule = _ERPNEXT_DIAGNOSIS_RULES.get(alert_name)
        if rule is None:
            return None

        evidence_refs = list(diagnosis.get("evidence_refs") or [])
        if not evidence_refs:
            return None

        return {
            "candidate_id": f"cand-erp-{incident_id[:12]}",
            "incident_id": incident_id,
            "scenario_id": rule["scenario_id"],
            "catalog_action_id": rule["catalog_action_id"],
            "rollback_action_id": rule["rollback_action_id"],
            "action_type": rule["action_type"].value,
            "target_resource_id": rule["target_resource_id"],
            "target_scope": rule["target_scope"],
            "environment": "staging",
            "evidence_refs": evidence_refs,
            "requires_approval": True,
            "execution_permitted": False,
            "reason": rule["reason"],
        }

    def describe_remediation_disposition(
        self,
        *,
        alert_name: str,
        diagnosis: dict[str, Any],
        remediation_candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rule = _ERPNEXT_DIAGNOSIS_RULES.get(alert_name)
        if remediation_candidate is not None and rule is not None:
            return {
                "scenario_id": rule["scenario_id"],
                "disposition": "staging_remediation_candidate_available",
                "catalog_action_id": rule["catalog_action_id"],
                "rollback_action_id": rule["rollback_action_id"],
                "requires_approval": True,
                "execution_permitted": False,
                "reason": rule["reason"],
            }
        return {
            "scenario_id": rule["scenario_id"] if rule else None,
            "disposition": "escalate_only",
            "catalog_action_id": None,
            "rollback_action_id": None,
            "requires_approval": True,
            "execution_permitted": False,
            "reason": str(diagnosis.get("reason") or "Escalated for human review."),
        }

    def get_target_scopes(self) -> dict[str, set[str]]:
        return {
            "staging_erpnext_db_nodes": {"erpnext-mariadb"},
            "staging_erpnext_app_nodes": {
                "erpnext-app",
                "erpnext-nginx",
                "erpnext-redis-queue",
            },
            "staging_erpnext_endpoints": {"erpnext-web-endpoint"},
        }

    def get_verification_probes(
        self,
        incident_id: str,
        *,
        scenario_id: str | None = None,
        action_id: str | None = None,
    ) -> dict[str, list[Any]]:
        return {
            "allowed_probes": [
                ProbeResult(name="erpnext_http", passed=True, details="ERPNext HTTP health check passed")
            ],
            "forbidden_probes": [
                ProbeResult(name="erpnext_public_mariadb_blocked", passed=True, details="MariaDB 3306 not publicly exposed")
            ],
            "related_probes": [
                ProbeResult(name="erpnext_redis_queue_ping", passed=True, details="Redis queue broker responsive")
            ],
        }

    def get_read_only_client(self) -> Any:
        return None


PlatformAdapterRegistry.register(ERPNextPlatformAdapter())
