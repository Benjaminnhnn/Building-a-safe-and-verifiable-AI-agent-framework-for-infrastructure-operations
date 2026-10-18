"""Cross-object validation that intentionally stays outside Pydantic models."""

from __future__ import annotations

from collections.abc import Iterable

from core.schema.action import TypedAction
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource


class ContractValidationError(ValueError):
    pass


def validate_pipeline_references(
    incident: Incident,
    action: TypedAction,
    evidence_items: Iterable[Evidence],
    resources: Iterable[Resource],
) -> None:
    evidence_by_id = {item.evidence_id: item for item in evidence_items}
    resources_by_id = {item.resource_id: item for item in resources}
    errors: list[str] = []

    if action.incident_id != incident.incident_id:
        errors.append("action incident_id does not match incident")

    foreign_evidence = sorted(
        item.evidence_id
        for item in evidence_by_id.values()
        if item.incident_id != incident.incident_id
    )
    if foreign_evidence:
        errors.append(f"evidence belongs to another incident: {foreign_evidence}")

    missing_evidence = sorted(set(action.evidence_refs) - set(evidence_by_id))
    if missing_evidence:
        errors.append(f"action references missing evidence: {missing_evidence}")

    target = resources_by_id.get(action.target_resource_id)
    if target is None:
        errors.append(f"target resource does not exist: {action.target_resource_id}")
    elif target.environment != action.environment:
        errors.append(
            "action environment does not match target environment: "
            f"{action.environment.value} != {target.environment.value}"
        )

    if errors:
        raise ContractValidationError("; ".join(errors))
