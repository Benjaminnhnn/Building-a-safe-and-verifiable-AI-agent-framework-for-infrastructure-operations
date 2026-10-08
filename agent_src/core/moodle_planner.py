"""Evidence-derived, read-only investigation plans for Moodle hypotheses.

This planner only creates operator-reviewable probe proposals. It cannot issue
remediation, pass a safety gate, or execute actions.
"""

from __future__ import annotations

import hashlib

from core.action_catalog import ActionCatalog
from core.moodle_contract import load_moodle_resource_inventory
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment


_INVESTIGATION_CATALOG = {
    "rds_path_probe_failed": (
        "postgres-db",
        "verify_tls_database_connection",
        "staging_moodle_nodes",
        "Collect a node-side Moodle-to-RDS connectivity probe; the existing alert probe is monitor-side only.",
    ),
    "node_rds_path_probe_failed": (
        "postgres-db",
        "verify_tls_database_connection",
        "staging_moodle_nodes",
        "Collect per-node Moodle-to-RDS connectivity and route evidence; a failed TCP path does not establish database failure.",
    ),
    "public_entry_probe_failed": (
        "moodle-web-endpoint",
        "probe_moodle_http_health",
        "moodle-web-endpoint",
        "Collect the catalogued Moodle HTTP health probe; it does not isolate individual ALB targets.",
    ),
}


def plan_moodle_investigation(
    *, incident_id: str, diagnosis: dict[str, object]
) -> TypedAction | None:
    """Create a bounded READ_HEALTH proposal for an isolated dependency signal."""
    cause_code = diagnosis.get("cause_code")
    contract = _INVESTIGATION_CATALOG.get(str(cause_code or ""))
    evidence_refs = diagnosis.get("evidence_refs")
    if (
        diagnosis.get("status") != "hypothesis"
        or contract is None
        or not isinstance(evidence_refs, list)
        or not evidence_refs
        or any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs)
    ):
        return None

    target_resource_id, catalog_action_id, target_scope, reason = contract
    catalog = ActionCatalog.load_moodle_catalog()
    catalog_entry = catalog.lookup(catalog_action_id)
    if (
        catalog_entry is None
        or catalog_entry.adapter != "probe"
        or not catalog_entry.permission.read_only
        or not catalog.is_allowed(
            catalog_action_id, environment=Environment.STAGING.value, role="verifier"
        )
    ):
        return None
    resource_types = {
        item["resource_id"]: item["type"]
        for item in load_moodle_resource_inventory().get("resources", [])
        if isinstance(item, dict)
        and isinstance(item.get("resource_id"), str)
        and isinstance(item.get("type"), str)
    }
    target_type = resource_types.get(target_resource_id)
    if target_type is None or not any(
        item.value == target_type
        for item in catalog_entry.valid_target_resource_types
    ):
        return None
    action_id = "act-" + hashlib.sha256(
        f"{incident_id}:{cause_code}:{':'.join(sorted(evidence_refs))}".encode()
    ).hexdigest()[:16]
    action = TypedAction(
        action_id=action_id,
        incident_id=incident_id,
        action_type=ActionType.READ_HEALTH,
        target_resource_id=target_resource_id,
        environment=Environment.STAGING,
        reason=reason,
        evidence_refs=sorted(set(evidence_refs)),
        parameters={
            "catalog_action_id": catalog_action_id,
            "target_scope": target_scope,
        },
        preconditions=[
            "operator reviews this read-only proposal",
            "target resource and staging scope are confirmed",
        ],
        expected_outcome="Collect new evidence only; this proposal cannot modify Moodle or AWS resources.",
        reversible=True,
        rollback_plan=RollbackPlan(
            available=False,
            method="Read-only proposal has no state change to roll back.",
        ),
        requires_approval=True,
    )
    action.idempotency_key = "idem-" + hashlib.sha256(action_id.encode()).hexdigest()[:16]
    return action
