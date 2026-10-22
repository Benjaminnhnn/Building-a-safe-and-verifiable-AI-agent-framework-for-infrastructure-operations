"""Evidence-derived investigation and bounded staging remediation proposals.

Mutation candidates are persisted for operator review but cannot pass the
Safety Gate, call the executor, or resolve an incident.
"""

from __future__ import annotations

import json
import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from core.safety.action_catalog import ActionCatalog
from core.moodle.contract import load_moodle_resource_inventory
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
    "web_container_not_running": (
        "moodle-app",
        "wait_for_alb_target_health",
        "moodle-app-b",
        "Collect a read-only ALB/container health probe to verify Moodle web container status.",
    ),
    "node_cpu_pressure": (
        "moodle-app",
        "verify_node_cpu_recovers",
        "moodle-app-b",
        "Collect a read-only node CPU probe to verify whether node CPU pressure is sustained.",
    ),
    "application_path_not_isolated": (
        "moodle-app",
        "probe_moodle_synthetic_login",
        "moodle-synthetic-transaction",
        "Collect a read-only synthetic login and dependency probe to isolate application-tier failure.",
    ),
    "apache_router_signal_missing": (
        "moodle-app",
        "probe_moodle_http_health",
        "staging_moodle_configuration",
        "Collect a read-only Moodle HTTP health probe to verify Apache router behavior.",
    ),
    "node_router_fallback_failed": (
        "moodle-app",
        "probe_moodle_http_health",
        "staging_moodle_configuration",
        "Collect a read-only Moodle HTTP health probe to verify router fallback behavior.",
    ),
    "web_container_restarting": (
        "moodle-app",
        "wait_for_alb_target_health",
        "staging_moodle_app",
        "Collect a read-only target health probe while the web container restart cause is investigated.",
    ),
    "node_rds_tcp_latency_high": (
        "postgres-db",
        "verify_tls_database_connection",
        "staging_moodle_nodes",
        "Collect a read-only TLS database connectivity probe to inspect node-to-RDS latency.",
    ),
    "trusted_proxy_setting_flagged_for_review": (
        "moodle-app",
        "probe_moodle_http_health",
        "staging_moodle_configuration",
        "Collect a read-only HTTP probe while operator reviews reverse-proxy trust configuration.",
    ),
}

_S3_3_REMEDIATIONS = {
    "DB-01": ("MoodleNodeRdsTcpFailed", "remove_scoped_db_reject", "postgres-db", "staging_moodle_nodes"),
    "RES-01": ("MoodleNodeCpuHigh", "remove_named_cpu_load_container", "moodle-app", "moodle-app-b"),
    "NET-01": ("MoodleSyntheticTransactionFailed", "recreate_moodle_web_from_reviewed_compose", "moodle-app", "staging_moodle_nodes"),
    "CON-01": ("MoodleWebContainerMissing", "start_reviewed_compose_service", "moodle-app", "moodle-app-b"),
    "SEC-02": ("MoodleSyntheticTransactionFailed", "restore_fixture_directory_mode", "moodledata-volume", "moodledata_synthetic_fixture_directory"),
}
_S3_3_DIAGNOSES = {
    "DB-01": "node_rds_path_probe_failed",
    "RES-01": "node_cpu_pressure",
    "NET-01": "application_path_not_isolated",
    "CON-01": "web_container_not_running",
    "SEC-02": "application_path_not_isolated",
}

SCENARIO_REMEDIATION_DISPOSITION: dict[str, dict[str, str]] = {
    "DB-01": {
        "disposition": "s3_3_live_allowlisted",
        "catalog_action_id": "remove_scoped_db_reject",
        "target_scope": "staging_moodle_nodes",
        "reason": "Allowlisted S3.3 staging remediation candidate when exact tagged rule and failed node RDS probes match.",
    },
    "DB-02": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "restore_database_connection_capacity",
        "target_scope": "staging_rds_postgresql",
        "reason": "Escalated for operator review; DB connection capacity recovery is outside the S3.3 live execution slice.",
    },
    "DB-03": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "restore_approved_database_endpoint",
        "target_scope": "staging_moodle_configuration",
        "reason": "Escalated for operator review; database endpoint drift requires reviewed configuration reconciliation.",
    },
    "RES-01": {
        "disposition": "s3_3_live_allowlisted",
        "catalog_action_id": "remove_named_cpu_load_container",
        "target_scope": "moodle-app-b",
        "reason": "Allowlisted S3.3 staging remediation candidate when CPU threshold and named fixture probes match.",
    },
    "RES-02": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "remove_named_memory_pressure_container",
        "target_scope": "moodle-app-b",
        "reason": "Escalated for operator review; memory pressure telemetry does not isolate a live-approved remediation.",
    },
    "RES-03": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "remove_named_disk_fixture",
        "target_scope": "moodle-app-b",
        "reason": "Escalated for operator review; isolated scratch ENOSPC requires operator verification before cleanup.",
    },
    "NET-01": {
        "disposition": "s3_3_live_allowlisted",
        "catalog_action_id": "recreate_moodle_web_from_reviewed_compose",
        "target_scope": "staging_moodle_nodes",
        "reason": "Allowlisted S3.3 staging remediation candidate when hosts-override marker and dependency probes match.",
    },
    "NET-02": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "remove_scoped_moodle_db_port_reject",
        "target_scope": "staging_moodle_nodes",
        "reason": "Escalated for operator review; DB port block without DB-01 tag is outside the S3.3 live execution slice.",
    },
    "NET-03": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "remove_scoped_netem_profile",
        "target_scope": "staging_moodle_nodes",
        "reason": "Escalated for operator review; RDS TCP latency profile removal requires operator confirmation.",
    },
    "CON-01": {
        "disposition": "s3_3_live_allowlisted",
        "catalog_action_id": "start_reviewed_compose_service",
        "target_scope": "moodle-app-b",
        "reason": "Allowlisted S3.3 staging remediation candidate when stopped web container and healthy host probes match.",
    },
    "CON-02": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "restore_moodle_apache_router",
        "target_scope": "moodle-app-b",
        "reason": "Escalated for operator review; Apache router restoration is outside the S3.3 live execution slice.",
    },
    "CON-03": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "restore_approved_runtime_env",
        "target_scope": "moodle-app-b",
        "reason": "Escalated for operator review; container restart loop and router fallback require operator isolation.",
    },
    "SEC-01": {
        "disposition": "human_only_escalation",
        "catalog_action_id": "remove_tagged_database_ingress_rule",
        "target_scope": "staging_database_security_group",
        "reason": "Human-only escalation: HIGH blast-radius AWS RDS security-group ingress modification is never agent-executable.",
    },
    "SEC-02": {
        "disposition": "s3_3_live_allowlisted",
        "catalog_action_id": "restore_fixture_directory_mode",
        "target_scope": "moodledata_synthetic_fixture_directory",
        "reason": "Allowlisted S3.3 staging remediation candidate when fixture directory mode-zero marker and dependency probes match.",
    },
    "SEC-03": {
        "disposition": "escalated_shadow_only",
        "catalog_action_id": "restore_approved_proxy_configuration",
        "target_scope": "staging_moodle_configuration",
        "reason": "Escalated for operator review; trusted proxy configuration drift requires human baseline verification.",
    },
}


# Authentication-layer dispositions are kept apart from the 15 Moodle fault
# scenarios. Identity configuration changes are human-only: the catalog action
# is operator-role, has no reviewed rollback, and is absent from the S3.3
# execution catalog and the executor wrapper, so every automated path fails
# closed.
AUTH_REMEDIATION_DISPOSITION: dict[str, dict[str, str]] = {
    "MoodleSsoLoginOptionMissing": {
        "cause_code": "sso_login_option_missing",
        "disposition": "human_only_escalation",
        "scenario_id": "SSO-13",
        "catalog_action_id": "restore_moodle_oauth2_issuer",
        "target_scope": "staging_moodle_oauth2_issuer",
        "reason": (
            "Human-only escalation: an operator must re-enable or repair the Moodle OAuth 2 (Okta) "
            "issuer; identity configuration is never agent-executable."
        ),
    },
}


def plan_moodle_s3_3_remediation(
    *, incident_id: str, alert_name: str, diagnosis: dict[str, Any], evidence: list[Any]
) -> dict[str, Any] | None:
    """Propose only a fault whose exact, fresh runtime marker matches its alert."""
    rows: dict[str, dict[str, tuple[float, str, datetime]]] = {}
    observed_times: list[datetime] = []
    evidence_ids: set[str] = set()
    now = datetime.now(timezone.utc)
    for item in evidence:
        if getattr(item, "source", None) != "prometheus":
            continue
        collected_at = getattr(item, "collected_at", None)
        if not isinstance(collected_at, datetime) or collected_at.tzinfo is None:
            continue
        age = (now - collected_at.astimezone(timezone.utc)).total_seconds()
        metadata = getattr(item, "metadata", None)
        try:
            payload = json.loads(metadata.get("payload_json", "{}"))
        except (AttributeError, TypeError, ValueError):
            continue
        query_id, value = payload.get("query_id"), payload.get("sample_value")
        labels = payload.get("labels")
        instance = labels.get("instance") if isinstance(labels, dict) else None
        evidence_id = getattr(item, "evidence_id", None)
        if (
            not isinstance(query_id, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not isinstance(instance, str)
            or not isinstance(evidence_id, str)
            or not evidence_id
            or age > 120
            or age < -30
        ):
            continue
        node_rows = rows.setdefault(query_id, {})
        if instance in node_rows:
            return None
        node_rows[instance] = (float(value), evidence_id, collected_at)
        observed_times.append(collected_at.astimezone(timezone.utc))

    if not observed_times or (max(observed_times) - min(observed_times)).total_seconds() > 60:
        return None
    if diagnosis.get("status") != "hypothesis":
        return None
    diagnosis_refs = diagnosis.get("evidence_refs")
    if not isinstance(diagnosis_refs, list) or not diagnosis_refs or any(
        not isinstance(ref, str) or not ref for ref in diagnosis_refs
    ):
        return None

    def matches(query_id: str, expected: dict[str, float] | None = None, *, minimum: float | None = None) -> bool:
        samples = rows.get(query_id, {})
        if expected is not None:
            if set(samples) != set(expected) or any(samples[node][0] != value for node, value in expected.items()):
                return False
        elif minimum is not None:
            if (
                set(samples) != {"moodle-app-a", "moodle-app-b"}
                or samples["moodle-app-a"][0] > minimum
                or samples["moodle-app-b"][0] <= minimum
            ):
                return False
        evidence_ids.update(sample[1] for sample in samples.values())
        return True

    candidates: list[str] = []
    if alert_name == _S3_3_REMEDIATIONS["DB-01"][0] and (
        matches("moodle_node_rds_tcp_success", {"moodle-app-a": 0.0, "moodle-app-b": 0.0})
        and matches("moodle_fault_db01_tagged_rule", {"moodle-app-a": 1.0, "moodle-app-b": 1.0})
    ):
        candidates.append("DB-01")
    if alert_name == _S3_3_REMEDIATIONS["RES-01"][0] and (
        matches("moodle_node_cpu_busy_percent", minimum=70.0)
        and matches("moodle_fault_res01_cpu_fixture_running", {"moodle-app-a": 0.0, "moodle-app-b": 1.0})
        and matches("moodle_node_web_running", {"moodle-app-a": 1.0, "moodle-app-b": 1.0})
    ):
        candidates.append("RES-01")
    if alert_name == _S3_3_REMEDIATIONS["CON-01"][0] and matches(
        "moodle_fault_con01_web_stopped", {"moodle-app-a": 0.0, "moodle-app-b": 1.0}
    ) and matches("moodle_node_web_running", {"moodle-app-a": 1.0, "moodle-app-b": 0.0}) and matches(
        "moodle_node_up", {"moodle-app-a": 1.0, "moodle-app-b": 1.0}
    ) and matches(
        "moodle_cadvisor_up", {"moodle-app-a": 1.0, "moodle-app-b": 1.0}
    ):
        candidates.append("CON-01")
    if alert_name == _S3_3_REMEDIATIONS["NET-01"][0]:
        deps_healthy = (
            matches("moodle_synthetic_success", {"monitor-ai-01": 0.0})
            and matches("moodle_rds_tcp_probe", {"monitor-ai-01": 1.0})
            and matches("moodle_public_probe", {"monitor-ai-01": 1.0})
            and matches("moodle_efs_mount", {"moodle-app-a": 1.0, "moodle-app-b": 1.0})
        )
        if deps_healthy and matches("moodle_fault_net01_hosts_override", {"moodle-app-a": 1.0, "moodle-app-b": 1.0}):
            candidates.append("NET-01")
        if deps_healthy and matches("moodle_fault_sec02_fixture_mode_zero", {"moodle-app-a": 1.0, "moodle-app-b": 0.0}):
            candidates.append("SEC-02")
    if (
        len(candidates) != 1
        or diagnosis.get("cause_code") != _S3_3_DIAGNOSES[candidates[0]]
        or not set(diagnosis_refs).issubset(evidence_ids)
        or len(evidence_ids) < 3
    ):
        return None

    scenario_id = candidates[0]
    _, catalog_action_id, target_resource_id, target_scope = _S3_3_REMEDIATIONS[scenario_id]
    catalog = ActionCatalog.load_moodle_s3_3_execution_catalog()
    entry = catalog.lookup(catalog_action_id)
    if entry is None or not entry.rollback_action or entry.action_type is None:
        return None
    evidence_refs = sorted(evidence_ids)
    action_id = "act-" + hashlib.sha256(
        f"{incident_id}:{scenario_id}:{catalog_action_id}:{':'.join(evidence_refs)}".encode()
    ).hexdigest()[:16]
    action = TypedAction(
        action_id=action_id,
        incident_id=incident_id,
        action_type=entry.action_type,
        target_resource_id=target_resource_id,
        environment=Environment.STAGING,
        reason=f"Fresh exact runtime probes match the scoped {scenario_id} fault contract.",
        evidence_refs=evidence_refs,
        preconditions=[
            "operator confirms the current staging maintenance window",
            "Safety Gate and independent executor approval are required before dispatch",
        ],
        expected_outcome="Reapply the reviewed staging baseline; an independent verifier must confirm recovery.",
        reversible=True,
        rollback_plan=RollbackPlan(
            available=True,
            rollback_action_id=entry.rollback_action,
            method=f"Reapply the reviewed safe baseline using {entry.rollback_action}.",
        ),
        requires_approval=True,
    )
    action.idempotency_key = "idem-" + hashlib.sha256(action_id.encode()).hexdigest()[:16]
    return {
        "scenario_id": scenario_id,
        "catalog_action_id": catalog_action_id,
        "target_scope": target_scope,
        "evidence_refs": evidence_refs,
        "action": action.model_dump(mode="json"),
        "approval_required": True,
        "execution_permitted": False,
        "resolution_eligible": False,
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


def build_evidence_provenance(
    evidence: list[Any], *, evidence_refs: list[str] | None = None
) -> list[dict[str, str]]:
    """Return deterministic source and timestamp provenance for referenced evidence IDs."""
    by_id: dict[str, dict[str, str]] = {}
    allowed_refs = set(evidence_refs) if evidence_refs is not None else None
    for item in evidence:
        evidence_id = getattr(item, "evidence_id", None)
        if not isinstance(evidence_id, str) or not evidence_id:
            continue
        if allowed_refs is not None and evidence_id not in allowed_refs:
            continue
        collected_at = getattr(item, "collected_at", None)
        collected_str = (
            collected_at.astimezone(timezone.utc).isoformat()
            if isinstance(collected_at, datetime) and collected_at.tzinfo is not None
            else str(collected_at or "unknown")
        )
        by_id[evidence_id] = {
            "evidence_id": evidence_id,
            "source": str(getattr(item, "source", "unknown")),
            "collected_at": collected_str,
            "resource_id": str(getattr(item, "resource_id", "unknown")),
        }
    return [by_id[ref] for ref in sorted(by_id)]


def describe_moodle_remediation_disposition(
    *,
    alert_name: str,
    diagnosis: dict[str, Any],
    remediation_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe whether an alert/diagnosis yields an S3.3 candidate or safe escalation."""
    if remediation_candidate is not None:
        scenario_id = str(remediation_candidate.get("scenario_id") or "")
        info = SCENARIO_REMEDIATION_DISPOSITION.get(scenario_id, {})
        return {
            "disposition": "candidate_proposed",
            "scenario_id": scenario_id,
            "catalog_action_id": remediation_candidate.get("catalog_action_id"),
            "target_scope": remediation_candidate.get("target_scope"),
            "reason": info.get(
                "reason",
                f"Exact runtime probes match the allowlisted {scenario_id} staging fault contract.",
            ),
            "execution_permitted": False,
            "approval_required": True,
        }

    status = str(diagnosis.get("status") or "insufficient_evidence")
    cause_code = str(diagnosis.get("cause_code") or "")
    if status != "hypothesis":
        return {
            "disposition": "awaiting_valid_evidence",
            "scenario_id": None,
            "catalog_action_id": None,
            "target_scope": None,
            "reason": str(
                diagnosis.get("reason")
                or "Diagnosis is not a confirmed hypothesis; failing closed without remediation candidate."
            ),
            "execution_permitted": False,
            "approval_required": False,
        }

    auth_info = AUTH_REMEDIATION_DISPOSITION.get(alert_name)
    if auth_info is not None and cause_code == auth_info["cause_code"]:
        return {
            "disposition": auth_info["disposition"],
            "scenario_id": auth_info["scenario_id"],
            "catalog_action_id": auth_info["catalog_action_id"],
            "target_scope": auth_info["target_scope"],
            "cause_code": cause_code,
            "reason": auth_info["reason"],
            "execution_permitted": False,
            "approval_required": True,
        }

    if alert_name == "MoodleDatabasePortExposure":
        info = SCENARIO_REMEDIATION_DISPOSITION["SEC-01"]
        return {
            "disposition": info["disposition"],
            "scenario_id": "SEC-01",
            "catalog_action_id": info["catalog_action_id"],
            "target_scope": info["target_scope"],
            "reason": info["reason"],
            "execution_permitted": False,
            "approval_required": True,
        }

    return {
        "disposition": "escalated_awaiting_operator_isolation",
        "scenario_id": None,
        "catalog_action_id": None,
        "target_scope": None,
        "cause_code": cause_code,
        "reason": (
            f"Hypothesis '{cause_code}' for alert '{alert_name}' does not match an exact S3.3 live-allowlisted "
            "fault marker; escalating for read-only investigation or human operator review."
        ),
        "execution_permitted": False,
        "approval_required": False,
    }

