"""Bounded read-only evidence collectors used by the shadow observer."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.evidence_store import evidence_content_hash
from core.schema.evidence import Evidence

@dataclass(frozen=True)
class EvidenceDraft:
    source: str
    resource_id: str
    kind: str
    observed_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class CollectionBatch:
    evidence: list[EvidenceDraft]
    errors: list[str]


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|"
    r"aws_secret_access_key)\b(\s*[:=]\s*)((?:Bearer\s+)?[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
_URL_CREDENTIALS = re.compile(r"(://)[^/@\s]+:[^/@\s]+@")


def sanitize_log_excerpt(value: Any, *, max_chars: int = 1024) -> str:
    """Redact common credentials and bound the copied log context."""
    text = str(value or "")
    text = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _AWS_ACCESS_KEY.sub("[REDACTED_AWS_ACCESS_KEY]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    return text[:max_chars]


def alert_context_evidence(
    alert: dict[str, Any], *, resource_id: str, incident_id: str
) -> EvidenceDraft:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    alert_name = str(labels.get("alertname") or "unknown")[:120]
    is_log_alert = alert_name == "LogFileErrorDetected"
    description = annotations.get("description")
    context = description if is_log_alert and description else annotations.get("summary")
    source = "log_watcher" if is_log_alert and description else "alertmanager"
    kind = "sanitized_log_excerpt" if source == "log_watcher" else "alert_context"
    payload = {
        "alertname": alert_name,
        "status": str(alert.get("status") or "unknown")[:24],
        "severity": str(labels.get("severity") or "unknown")[:24],
        "context": sanitize_log_excerpt(context),
        "redaction_policy": "sprint4-redaction-v1",
    }
    starts_at = alert.get("startsAt")
    observed_at = datetime.now(timezone.utc)
    if isinstance(starts_at, str):
        try:
            parsed = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                observed_at = parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return EvidenceDraft(source, resource_id, kind, observed_at, payload)


METRIC_QUERIES_BY_ALERT: dict[str, tuple[tuple[str, str], ...]] = {
    "MoodleNodeExporterDown": (("moodle_node_up", 'up{job="moodle_node"}'),),
    "MoodleCAdvisorDown": (("moodle_cadvisor_up", 'up{job="moodle_cadvisor"}'),),
    "MoodlePublicProbeFailed": (
        ("moodle_public_probe", 'probe_success{job="blackbox_moodle_public"}'),
    ),
    "MoodleRdsTcpProbeFailed": (
        ("moodle_rds_tcp_probe", 'probe_success{job="blackbox_moodle_rds"}'),
    ),
    "MoodleEfsMountMissing": (
        ("moodle_efs_mount", 'moodle_efs_mount_available{job="moodle_node"}'),
    ),
    "MoodleSyntheticTransactionFailed": (
        (
            "moodle_synthetic_success",
            'moodle_synthetic_success{job="moodle_synthetic",instance="monitor-ai-01"}',
        ),
        ("moodle_rds_tcp_probe", 'probe_success{job="blackbox_moodle_rds"}'),
        ("moodle_public_probe", 'probe_success{job="blackbox_moodle_public"}'),
        ("moodle_efs_mount", 'moodle_efs_mount_available{job="moodle_node"}'),
    ),
    "MoodleSyntheticTransactionStale": (
        (
            "moodle_synthetic_last_run_age_seconds",
            'time() - moodle_synthetic_last_run_timestamp_seconds{job="moodle_synthetic",instance="monitor-ai-01"}',
        ),
    ),
    "MoodleNodeCpuHigh": (
        (
            "moodle_node_cpu_busy_percent",
            '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{job="moodle_node",mode="idle"}[30s])))',
        ),
    ),
    "MoodleWebContainerMissing": (
        (
            "moodle_web_container_last_seen_age_seconds",
            'time() - max by (instance) (container_last_seen{job="moodle_cadvisor",name="release-moodle-web-1"})',
        ),
    ),
}

_SAFE_LABELS = frozenset({"job", "instance", "resource_id", "environment"})


class PrometheusEvidenceCollector:
    """Query a finite set of pre-reviewed PromQL expressions; never accepts user PromQL."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 1.5,
        max_sample_age_seconds: int = 120,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Prometheus URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Prometheus URL must not contain credentials or query data")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_sample_age_seconds = max_sample_age_seconds
        self.client = client

    def collect(self, alert_name: str, *, resource_id: str) -> CollectionBatch:
        definitions = METRIC_QUERIES_BY_ALERT.get(alert_name)
        if definitions is None:
            return CollectionBatch([], ["unsupported_alert_metric_contract"])

        collected: list[EvidenceDraft] = []
        errors: list[str] = []
        for query_id, expression in definitions:
            try:
                collected.extend(self._collect_query(query_id, expression, resource_id))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{query_id}:{type(exc).__name__}")
        return CollectionBatch(collected, errors)

    def _collect_query(
        self, query_id: str, expression: str, resource_id: str
    ) -> list[EvidenceDraft]:
        if self.client is None:
            response = httpx.get(
                f"{self.base_url}/api/v1/query",
                params={"query": expression},
                timeout=self.timeout_seconds,
            )
        else:
            response = self.client.get(
                f"{self.base_url}/api/v1/query",
                params={"query": expression},
                timeout=self.timeout_seconds,
            )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success":
            raise ValueError("prometheus_query_failed")
        results = body.get("data", {}).get("result", [])
        if not isinstance(results, list):
            raise ValueError("prometheus_result_malformed")

        now = datetime.now(timezone.utc)
        evidence: list[EvidenceDraft] = []
        if not results:
            evidence.append(
                EvidenceDraft(
                    "prometheus",
                    resource_id,
                    "metric_absence",
                    now,
                    {"query_id": query_id, "sample_status": "series_absent"},
                )
            )
            return evidence

        for item in results[:20]:
            timestamp, raw_value = item["value"]
            sample_time = float(timestamp)
            value = float(raw_value)
            age = now.timestamp() - sample_time
            if not math.isfinite(value) or age < -30 or age > self.max_sample_age_seconds:
                raise ValueError("prometheus_sample_invalid_or_stale")
            raw_labels = item.get("metric", {})
            labels = {
                str(key): sanitize_log_excerpt(val, max_chars=120)
                for key, val in raw_labels.items()
                if key in _SAFE_LABELS
            }
            evidence.append(
                EvidenceDraft(
                    "prometheus",
                    resource_id,
                    "metric_sample",
                    datetime.fromtimestamp(sample_time, tz=timezone.utc),
                    {
                        "query_id": query_id,
                        "sample_value": value,
                        "sample_age_seconds": round(max(age, 0.0), 3),
                        "labels": labels,
                    },
                )
            )
        return evidence


def static_inventory_evidence(
    resource_id: str,
    *,
    inventory_path: Path | None = None,
) -> CollectionBatch:
    path = inventory_path or Path(__file__).resolve().parents[1] / "config" / "moodle_resource_inventory.json"
    try:
        raw = path.read_bytes()
        inventory = json.loads(raw)
        resources = inventory["resources"]
        resource = next(item for item in resources if item["resource_id"] == resource_id)
    except (OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
        return CollectionBatch([], [f"inventory:{type(exc).__name__}"])

    now = datetime.now(timezone.utc)
    inventory_hash = hashlib.sha256(raw).hexdigest()
    config = EvidenceDraft(
        "resource_inventory",
        resource_id,
        "static_config_snapshot",
        now,
        {
            "inventory_sha256": inventory_hash,
            "resource_type": str(resource.get("type", "unknown")),
            "environment": str(resource.get("environment", "unknown")),
            "service_name": str(resource.get("service_name") or "unknown"),
            "snapshot_scope": "packaged_logical_inventory_not_runtime_host_config",
        },
    )
    topology = EvidenceDraft(
        "resource_inventory",
        resource_id,
        "topology_snapshot",
        now,
        {
            "inventory_sha256": inventory_hash,
            "depends_on": sorted(str(item) for item in resource.get("depends_on", [])),
            "snapshot_scope": "packaged_logical_topology_not_runtime_discovery",
        },
    )
    return CollectionBatch([config, topology], [])


def collect_shadow_evidence(
    alert: dict[str, Any],
    *,
    incident_id: str,
    resource_id: str,
    evidence_store: Any,
    prometheus_url: str | None,
) -> dict[str, Any]:
    labels = alert.get("labels") or {}
    alert_name = str(labels.get("alertname") or "unknown")
    batches = [
        CollectionBatch(
            [alert_context_evidence(alert, resource_id=resource_id, incident_id=incident_id)],
            [],
        ),
        static_inventory_evidence(resource_id),
    ]
    if prometheus_url:
        try:
            batches.append(
                PrometheusEvidenceCollector(prometheus_url).collect(
                    alert_name, resource_id=resource_id
                )
            )
        except ValueError as exc:
            batches.append(CollectionBatch([], [f"prometheus_config:{type(exc).__name__}"]))
    else:
        batches.append(CollectionBatch([], ["prometheus_config:missing"]))

    evidence_refs: list[str] = []
    errors: list[str] = []
    sources: set[str] = set()
    existing_inventory_snapshots: set[tuple[str, str]] = set()
    for existing in evidence_store.list_for_incident(incident_id):
        if existing.source != "resource_inventory":
            continue
        try:
            payload = json.loads(existing.metadata.get("payload_json", "{}"))
        except (TypeError, ValueError):
            continue
        inventory_hash = str(payload.get("inventory_sha256") or "")
        if inventory_hash:
            existing_inventory_snapshots.add(
                (str(existing.metadata.get("kind") or ""), inventory_hash)
            )
    for batch in batches:
        errors.extend(batch.errors)
        for draft in batch.evidence:
            payload_json = json.dumps(
                draft.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            identity = hashlib.sha256(
                ":".join(
                    (
                        incident_id,
                        draft.source,
                        draft.resource_id,
                        draft.kind,
                        draft.observed_at.isoformat(),
                        payload_json,
                    )
                ).encode("utf-8")
            ).hexdigest()
            if draft.source == "resource_inventory" and (
                draft.kind,
                str(draft.payload.get("inventory_sha256") or ""),
            ) in existing_inventory_snapshots:
                continue
            evidence = Evidence(
                evidence_id=f"ev-{identity[:24]}",
                incident_id=incident_id,
                resource_id=draft.resource_id,
                source=draft.source,
                collected_at=draft.observed_at,
                summary=payload_json[:4096],
                raw_ref=f"{draft.source}:{draft.kind}",
                content_hash="pending",
                metadata={"kind": draft.kind, "payload_json": payload_json[:4096]},
                redacted=True,
            )
            evidence.content_hash = evidence_content_hash(evidence)
            evidence_store.append(evidence)
            evidence_refs.append(evidence.evidence_id)
            sources.add(draft.source)
            if draft.source == "resource_inventory":
                existing_inventory_snapshots.add(
                    (draft.kind, str(draft.payload.get("inventory_sha256") or ""))
                )

    return {
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "sources": sorted(sources),
        "errors": errors,
        "status": "complete" if not errors else "partial",
    }
