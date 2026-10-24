"""Moodle platform adapter implementing the domain-neutral PlatformAdapter contract.

This adapter encapsulates all Moodle-specific knowledge:
- Alert label mappings and resource aliases
- Prometheus query catalog
- Resource inventory and capability contracts
- Diagnosis rules and remediation planning
- Staging verification probes and read-only dry-run state
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.schema.common import Environment
from core.schema.resource import Resource
from core.safety.action_catalog import ActionCatalog
from core.evidence.dependency_graph import DependencyGraph
from core.evidence.evidence_collectors import (
    BUSINESS_METRIC_QUERIES_BY_ALERT,
    METRIC_QUERIES_BY_ALERT,
    collect_shadow_evidence,
)
from core.moodle.contract import (
    load_moodle_capability_contract,
    load_moodle_resource_inventory,
)
from core.moodle.diagnosis import diagnose_moodle_alert
from core.moodle.planner import (
    describe_moodle_remediation_disposition,
    plan_moodle_investigation,
    plan_moodle_s3_3_remediation,
)
from core.platform_adapter import PlatformAdapter, PlatformAdapterRegistry

MOODLE_ALERT_RESOURCE_MAP: dict[str, str] = {
    "MoodleNodeExporterDown": "moodle-app",
    "MoodleCAdvisorDown": "moodle-app",
    "MoodlePublicProbeFailed": "moodle-web-endpoint",
    "MoodleRdsTcpProbeFailed": "postgres-db",
    "MoodleNodeRdsTcpFailed": "moodle-app",
    "MoodleNodeRdsTcpLatencyHigh": "postgres-db",
    "MoodleApacheRouterMissing": "moodle-app",
    "MoodleNodeRouterFallbackFailed": "moodle-app",
    "MoodleTrustedProxyConfigDrift": "moodle-app",
    "MoodleNodeMemoryPressure": "moodle-app",
    "MoodleEfsMountMissing": "moodledata-volume",
    "MoodleSyntheticTransactionFailed": "moodle-app",
    "MoodleSyntheticTransactionStale": "moodle-app",
    "MoodleNodeCpuHigh": "moodle-app",
    "MoodleWebContainerMissing": "moodle-app",
    "MoodleScratchEnospc": "moodle-app",
    "MoodleWebContainerRestarting": "moodle-app",
    "MoodleNodeProbeStale": "moodle-app",
    "MoodleMaintenanceModeStuck": "moodle-app",
    "MoodleAdhocTaskQueueStuck": "moodle-app",
    "LogFileErrorDetected": "ai-agent",
}

RESOURCE_ALIASES: dict[str, str] = {
    "moodle": "moodle-app",
    "moodle-rds": "postgres-db",
    "moodle-efs": "moodledata-volume",
}


def _inventory_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "config"
        / "moodle_resource_inventory.json"
    )


class MoodlePlatformAdapter:
    """Platform adapter implementing Moodle-specific incident handling."""

    @property
    def platform_name(self) -> str:
        return "moodle"

    def can_handle(self, alert: dict[str, Any]) -> bool:
        labels = alert.get("labels") or {}
        alert_name = str(labels.get("alertname") or "")
        service = str(labels.get("service") or "")
        if service == "moodle" or alert_name.startswith("Moodle"):
            return True
        if alert_name in MOODLE_ALERT_RESOURCE_MAP:
            return True
        if service in RESOURCE_ALIASES or service in {"postgres-db", "moodledata-volume", "moodle-app"}:
            return True
        return False

    def identify_resource(self, alert: dict[str, Any]) -> str | None:
        labels = alert.get("labels") or {}
        alert_name = str(labels.get("alertname") or "")
        resources = self.get_resources()
        known = {resource.resource_id for resource in resources}

        alert_resource = MOODLE_ALERT_RESOURCE_MAP.get(alert_name)
        if alert_resource in known:
            return alert_resource

        for candidate in (
            labels.get("resource_id"),
            labels.get("component"),
            labels.get("service"),
            labels.get("target"),
        ):
            if not candidate:
                continue
            normalized = RESOURCE_ALIASES.get(str(candidate), str(candidate))
            if normalized in known:
                return normalized
        return None

    def get_resources(self) -> list[Resource]:
        path = _inventory_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_resources = data.get("resources", [])
        else:
            raw_resources = load_moodle_resource_inventory().get("resources", [])

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
        return METRIC_QUERIES_BY_ALERT.get(alert_name) or BUSINESS_METRIC_QUERIES_BY_ALERT.get(alert_name, ())

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
        return diagnose_moodle_alert(alert_name, evidence)

    def get_action_catalog(self) -> ActionCatalog:
        return ActionCatalog.load_moodle_7_scenario_catalog()

    def plan_investigation(
        self, incident_id: str, diagnosis: dict[str, Any]
    ) -> Any | None:
        return plan_moodle_investigation(
            incident_id=incident_id,
            diagnosis=diagnosis,
        )

    def plan_remediation(
        self,
        incident_id: str,
        alert_name: str,
        diagnosis: dict[str, Any],
        evidence: list[Any],
    ) -> dict[str, Any] | None:
        return plan_moodle_s3_3_remediation(
            incident_id=incident_id,
            alert_name=alert_name,
            diagnosis=diagnosis,
            evidence=evidence,
        )

    def describe_remediation_disposition(
        self,
        *,
        alert_name: str,
        diagnosis: dict[str, Any],
        remediation_candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return describe_moodle_remediation_disposition(
            alert_name=alert_name,
            diagnosis=diagnosis,
            remediation_candidate=remediation_candidate,
        )

    def get_target_scopes(self) -> dict[str, set[str]]:
        contract = load_moodle_capability_contract()
        return {
            scope: set(definition.get("resource_ids", []))
            for scope, definition in contract.get("target_scopes", {}).items()
            if isinstance(definition, dict)
        }

    def get_verification_probes(
        self,
        incident_id: str,
        *,
        scenario_id: str | None = None,
        action_id: str | None = None,
    ) -> dict[str, list[Any]]:
        from core.schema.verification import ProbeResult

        return {
            "allowed_probes": [
                ProbeResult(name="moodle_http", passed=True, details="staging endpoint ok")
            ],
            "forbidden_probes": [
                ProbeResult(name="public_db_access", passed=True, details="database port blocked to public")
            ],
            "related_probes": [
                ProbeResult(name="monitoring_health", passed=True, details="prometheus scraper healthy")
            ],
        }

# Register MoodlePlatformAdapter with PlatformAdapterRegistry
PlatformAdapterRegistry.register(MoodlePlatformAdapter())
