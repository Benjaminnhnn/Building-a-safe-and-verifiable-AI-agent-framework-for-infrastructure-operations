"""Observer module — normalizes alert streams into Incident and Evidence domain models.

Invariants:
1. Translates inbound Alertmanager or raw alert events into Phase 1 Incident and Evidence.
2. Generates deterministic, hash-stable correlation keys.
3. Every collected piece of Evidence has mandatory collected_at and expires_at.
4. Output envelope carries incident_id and evidence_ids.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.celery_app import celery_app
from evaluation.agent_replay.domain.enums import EvidenceSource
from evaluation.agent_replay.domain.evidence import Evidence
from evaluation.agent_replay.domain.incident import Incident, IncidentStatus
from evaluation.agent_replay.domain.resource import Resource
from evaluation.agent_replay.roles.contracts import BaseRoleMessage
from core.schema.event_schema import (
    normalize_alertmanager_payload,
    validate_alertmanager_payload,
)


class ObserverInput(BaseModel):
    """Input payload for the Observer role."""

    model_config = ConfigDict(extra="forbid")

    alert_payload: dict[str, Any] = Field(
        ...,
        description="Inbound Alertmanager alert payload or raw alert event dict.",
    )
    resources: list[Resource] = Field(
        default_factory=list,
        description="Known deployment resources to resolve targets against.",
    )
    incident_id: str | None = Field(
        default=None,
        description="Optional explicitly assigned incident identifier.",
    )


class ObserverOutput(BaseRoleMessage):
    """Output envelope from the Observer role."""

    incident: Incident = Field(..., description="Constructed domain Incident model.")
    evidence_items: list[Evidence] = Field(
        default_factory=list,
        description="Constructed typed Evidence domain records.",
    )
    signals: list[str] = Field(
        default_factory=list,
        description="Normalized signal names identified from alert events.",
    )


class ObserverRole:
    """Deterministic Observer component that builds initial Incident & Evidence records."""

    def observe(self, input_data: ObserverInput) -> ObserverOutput:
        raw_payload = input_data.alert_payload
        validation_errors = validate_alertmanager_payload(raw_payload)
        if validation_errors:
            # If standard Alertmanager validation fails, check if it's a single alert dict
            if not isinstance(raw_payload, dict) or ("alerts" not in raw_payload and "labels" not in raw_payload):
                raise ValueError(f"Invalid alert payload: {'; '.join(validation_errors)}")

        normalized = normalize_alertmanager_payload(raw_payload)
        alerts: list[dict[str, Any]] = normalized.get("alerts", [])
        if not alerts:
            # Fallback to single alert if raw payload contains alert labels directly
            if "labels" in raw_payload:
                alerts = [raw_payload]
            else:
                raise ValueError("No alerts found in normalized payload")

        resources_by_id = {r.resource_id: r for r in input_data.resources}

        first_alert = alerts[0]
        fingerprint = str(first_alert.get("fingerprint") or self._compute_fingerprint(first_alert))

        incident_id = input_data.incident_id or f"inc-{fingerprint[:16]}"
        resource_ids = self._resolve_resource_ids(alerts, resources_by_id)
        if not resource_ids:
            resource_ids = ["moodle-app"]

        starts_at_str = first_alert.get("startsAt")
        now = None
        if starts_at_str:
            try:
                now = datetime.fromisoformat(str(starts_at_str).replace("Z", "+00:00"))
            except Exception:
                pass
        if now is None:
            now = datetime.now(timezone.utc)

        evidence_items: list[Evidence] = []
        signals: list[str] = []

        for idx, alert in enumerate(alerts):
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            signal_name = str(
                alert.get("signal", {}).get("name")
                or labels.get("alertname")
                or labels.get("signal")
                or "system_alert"
            ).strip().lower()
            signals.append(signal_name)

            content_digest = hashlib.sha256(
                f"{incident_id}:{signal_name}:{idx}".encode("utf-8")
            ).hexdigest()[:16]
            evidence_id = f"evi-{content_digest}"

            summary = str(
                annotations.get("summary")
                or annotations.get("description")
                or f"Observed alert signal {signal_name}"
            )

            # Assign valid expiry (1 hour TTL)
            item = Evidence(
                evidence_id=evidence_id,
                source=EvidenceSource.METRIC,
                collected_at=now,
                expires_at=now + timedelta(hours=1),
                resource_ids=resource_ids,
                content_ref=f"metric:{signal_name}",
                summary=summary,
            )
            evidence_items.append(item)

        evidence_ids = [e.evidence_id for e in evidence_items]

        incident = Incident(
            incident_id=incident_id,
            alert_fingerprint=fingerprint,
            status=IncidentStatus.OPEN,
            resource_ids=resource_ids,
            evidence_ids=evidence_ids,
            created_at=now,
            updated_at=now,
        )

        return ObserverOutput(
            incident_id=incident_id,
            evidence_ids=evidence_ids,
            incident=incident,
            evidence_items=evidence_items,
            signals=signals,
        )

    @staticmethod
    def _compute_fingerprint(alert: dict[str, Any]) -> str:
        labels = alert.get("labels", {})
        basis = f"{labels.get('alertname')}:{labels.get('instance')}:{labels.get('target')}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _resolve_resource_ids(
        alerts: list[dict[str, Any]],
        resources_by_id: dict[str, Resource],
    ) -> list[str]:
        candidates: set[str] = set()
        for alert in alerts:
            labels = alert.get("labels", {})
            for key in ("resource_id", "target", "service", "component_id"):
                val = labels.get(key) or alert.get(key)
                if val and str(val) in resources_by_id:
                    candidates.add(str(val))
                elif val and str(val).strip():
                    candidates.add(str(val).strip())
        return sorted(candidates)


@celery_app.task(name="evaluation.agent_replay.roles.observe_incident_task", bind=True)
def observe_incident_task(self: Any, alert_payload: dict[str, Any]) -> dict[str, Any]:
    """Celery background task for running the Observer role."""
    role = ObserverRole()
    output = role.observe(ObserverInput(alert_payload=alert_payload))
    return output.model_dump(mode="json")

