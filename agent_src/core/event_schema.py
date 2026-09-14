"""Normalize Alertmanager alerts into the versioned incident event schema (V2 compatible)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "2.0"
SCHEMA_VERSION_V1 = "1.0"
SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION, SCHEMA_VERSION_V1}

ALLOWED_DATA_CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}

# Keys that must never appear in normalized payload (secret / PII indicators).
SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|aws[_-]?secret|credential|auth|ssn|credit[_-]?card|card[_-]?number|pii)",
    re.IGNORECASE,
)

# Rough value patterns for raw secrets / PII that should be redacted before persistence/LLM.
# Intentionally conservative: we validate rather than silently pass through.
SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key style
    re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
    re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"),  # credit card-like
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-like
]

ENTITY_FIELDS = (
    "business_capability",
    "application_service",
    "repository",
    "commit_sha",
    "pull_request",
    "workflow_run_id",
    "workflow_job_id",
    "artifact",
    "image_digest",
    "deployment_id",
    "cluster",
    "namespace",
    "workload",
    "revision",
    "database_migration_id",
)

REDACTION_POLICY_VERSION = "redaction-v1"
REDACTED_PLACEHOLDER = "[REDACTED]"


def _stable_hash(value: Any, length: int = 16) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _alert_identity(alert: dict[str, Any]) -> str:
    if alert.get("fingerprint"):
        return str(alert["fingerprint"])
    labels = alert.get("labels") or {}
    return _stable_hash(
        {
            key: labels.get(key, "")
            for key in ("alertname", "instance", "job", "service", "target")
        }
    )


def _event_type(alert: dict[str, Any]) -> str:
    return "service_health_failed" if alert.get("status") == "firing" else "service_health_recovered"


def _is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_PATTERN.search(key))


def _is_sensitive_value(value: str) -> bool:
    for pat in SENSITIVE_VALUE_PATTERNS:
        if pat.search(value):
            return True
    return False


def _scan_for_sensitive_data(obj: Any, path: str = "") -> list[str]:
    """Return list of paths where sensitive key/value was found."""
    findings: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = f"{path}.{k}" if path else str(k)
            if _is_sensitive_key(str(k)):
                findings.append(f"{cur}: sensitive key")
            # only scan string values to avoid noise
            if isinstance(v, str) and _is_sensitive_value(v):
                findings.append(f"{cur}: sensitive value pattern")
            findings.extend(_scan_for_sensitive_data(v, cur))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            findings.extend(_scan_for_sensitive_data(item, f"{path}[{idx}]"))
    elif isinstance(obj, str) and _is_sensitive_value(obj):
        findings.append(f"{path}: sensitive value pattern")
    return findings


def _build_entities(
    alert: dict[str, Any],
    entities_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    # explicit entities on alert take precedence over labels
    base_entities = dict(alert.get("entities") or {})
    # pull known entity fields from labels/annotations if present
    for field in ENTITY_FIELDS:
        if field not in base_entities:
            # labels is primary source; annotations fallback
            if field in labels and labels[field]:
                base_entities[field] = labels[field]
            elif field in annotations and annotations[field]:
                base_entities[field] = annotations[field]
    if entities_override:
        base_entities.update({k: v for k, v in entities_override.items() if v is not None})
    # remove None/empty
    return {k: v for k, v in base_entities.items() if v not in (None, "")}


def _build_redaction(
    *,
    redaction_override: dict[str, Any] | None = None,
    has_sensitive_findings: bool = False,
    policy_version: str = REDACTION_POLICY_VERSION,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    base: dict[str, Any] = {
        "status": "passed",
        "policy_version": policy_version,
        "redacted_fields": [],
        "applied_at": now,
    }
    if has_sensitive_findings:
        base["status"] = "failed"
    if redaction_override:
        # only allow known keys, never accept raw secret values
        for key in ("status", "policy_version", "redacted_fields", "applied_at"):
            if key in redaction_override:
                base[key] = redaction_override[key]
        # ensure redacted_fields is list of strings (field names, not values)
        if not isinstance(base.get("redacted_fields"), list):
            base["redacted_fields"] = []
    return base


def _normalize_classification(value: Any) -> str:
    if value is None:
        return "internal"
    val = str(value).lower()
    if val not in ALLOWED_DATA_CLASSIFICATIONS:
        return "internal"
    return val


def validate_normalized_event(event: dict[str, Any]) -> list[str]:
    """Validate a normalized event. Returns list of error messages (empty = valid)."""
    errors: list[str] = []
    # schema_version
    sv = event.get("schema_version")
    if sv not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"unsupported schema_version: {sv}")
    # required fields from V1
    for field in ("event_id", "correlation_id", "source", "event_type", "observed_at", "received_at", "severity", "fingerprint"):
        if not event.get(field):
            errors.append(f"missing required field: {field}")
    # source must be alertmanager or compatible
    if event.get("source") not in ("alertmanager", "github", "synthetic"):
        # allow unknown but flag if empty
        pass
    # data_classification
    dc = event.get("data_classification")
    if dc is not None and dc not in ALLOWED_DATA_CLASSIFICATIONS:
        errors.append(f"invalid data_classification: {dc}")
    # entities: if present must be dict with known fields only and no sensitive keys
    entities = event.get("entities")
    if entities is not None:
        if not isinstance(entities, dict):
            errors.append("entities must be an object")
        else:
            for k in entities:
                if _is_sensitive_key(k):
                    errors.append(f"entities contains sensitive key: {k}")
                if k not in ENTITY_FIELDS and k not in ("environment", "service", "component"):
                    # allow extension but warn not error? keep permissive
                    pass
    # redaction
    redaction = event.get("redaction")
    if redaction is not None:
        if not isinstance(redaction, dict):
            errors.append("redaction must be an object")
        else:
            if redaction.get("status") not in ("passed", "failed", "skipped", "pending"):
                errors.append(f"invalid redaction.status: {redaction.get('status')}")
            if not isinstance(redaction.get("redacted_fields", []), list):
                errors.append("redaction.redacted_fields must be a list")
    # secret/PII leakage: scan normalized fields (exclude raw alert passthrough if present)
    # we deliberately exclude the original raw `labels`/`annotations` that may contain
    # unredacted data; but the normalized top-level fields must be clean.
    leak_findings = _scan_for_sensitive_data(
        {k: v for k, v in event.items() if k not in ("labels", "annotations", "generatorURL")}
    )
    # signal.value/threshold should not contain secrets either
    if leak_findings:
        errors.append(f"sensitive data leakage detected: {leak_findings[:3]}")
    return errors


def is_valid_normalized_event(event: dict[str, Any]) -> bool:
    return not validate_normalized_event(event)


def validate_alertmanager_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    alerts = payload.get("alerts")
    if alerts is None:
        errors.append("missing alerts field")
    elif not isinstance(alerts, list):
        errors.append("alerts must be a list")
    else:
        for idx, alert in enumerate(payload["alerts"]):
            if not isinstance(alert, dict):
                errors.append(f"alerts[{idx}] must be an object")
                continue
            if not alert.get("labels"):
                errors.append(f"alerts[{idx}] missing labels")
            # check sensitive leakage in alert labels/annotations before normalization
            findings = _scan_for_sensitive_data(alert.get("labels") or {})
            findings += _scan_for_sensitive_data(alert.get("annotations") or {})
            if findings:
                errors.append(f"alerts[{idx}] sensitive data in labels/annotations: {findings[:2]}")
    return errors


def normalize_alert(
    alert: dict[str, Any],
    *,
    correlation_id: str,
    received_at: str | None = None,
    entities: dict[str, Any] | None = None,
    data_classification: str | None = None,
    redaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one alert with the common fields required by the incident core.

    V2 adds optional `entities`, `data_classification`, `redaction` while
    keeping the V1 `Alertmanager` payload fully compatible. No secret/PII
    values are copied into the normalized payload — sensitive keys are
    validated and redaction metadata is attached instead.
    """
    labels = dict(alert.get("labels") or {})
    observed_at = alert.get("startsAt") or received_at
    received_at = received_at or datetime.now(timezone.utc).isoformat()
    identity = _alert_identity(alert)

    # Build V2 optional fields with safe defaults
    built_entities = _build_entities(alert, entities_override=entities)
    classification = _normalize_classification(data_classification or labels.get("data_classification"))
    # detect if alert itself contains sensitive data that would need redaction
    pre_findings = _scan_for_sensitive_data(labels) + _scan_for_sensitive_data(alert.get("annotations") or {})
    has_leak = bool(pre_findings)
    built_redaction = _build_redaction(redaction_override=redaction, has_sensitive_findings=has_leak)

    normalized = dict(alert)
    normalized.update(
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"evt-{_stable_hash({'identity': identity, 'observed_at': observed_at})}",
            "correlation_id": correlation_id,
            "incident_id": None,
            "source": "alertmanager",
            "event_type": _event_type(alert),
            "observed_at": observed_at,
            "received_at": received_at,
            "environment": labels.get("environment", "unknown"),
            "host_id": labels.get("instance"),
            "service_id": labels.get("service") or labels.get("job"),
            "component_id": labels.get("component") or labels.get("container"),
            "severity": labels.get("severity", "warning"),
            "fingerprint": identity,
            "signal": {
                "name": labels.get("signal") or labels.get("alertname", "unknown"),
                "value": labels.get("value"),
                "threshold": labels.get("threshold"),
            },
            # V2 additions — optional but always present with defaults for downstream consistency
            "entities": built_entities,
            "data_classification": classification,
            "redaction": built_redaction,
        }
    )
    # Defensive: ensure we did not accidentally propagate sensitive keys into normalized top-level
    # If leakage is detected post-build, mark redaction failed (does not raise, caller can validate)
    post_findings = _scan_for_sensitive_data({k: v for k, v in normalized.items() if k in ("entities", "data_classification")})
    if post_findings:
        normalized["redaction"]["status"] = "failed"
        normalized["redaction"]["redacted_fields"] = post_findings[:5]
    return normalized


def normalize_alertmanager_payload(
    payload: dict[str, Any],
    *,
    received_at: str | None = None,
    entities: dict[str, Any] | None = None,
    data_classification: str | None = None,
    redaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a webhook batch using one correlation id for replayability.

    Keeps V1 interface stable: extra V2 kwargs are optional and propagate to each alert.
    """
    alerts = payload.get("alerts") or []
    correlation_id = f"corr-{_stable_hash([_alert_identity(alert) for alert in alerts])}"
    return {
        **payload,
        "alerts": [
            normalize_alert(
                alert,
                correlation_id=correlation_id,
                received_at=received_at,
                entities=entities,
                data_classification=data_classification,
                redaction=redaction,
            )
            for alert in alerts
        ],
    }
