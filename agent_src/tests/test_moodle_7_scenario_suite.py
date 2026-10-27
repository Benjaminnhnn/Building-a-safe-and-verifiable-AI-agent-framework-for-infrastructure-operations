"""End-to-end test suite for the Focused 7-Scenario Moodle Benchmark (2 Business/Infra + 5 Technical).

Adheres strictly to the infrastructure boundary principles:
"Quản trị hạ tầng không quản trị ứng dụng" - AI does not mutate Moodle application
data, plugins, or gradebook tables.

7 Scenarios:
- 2 Business / Infrastructure Workflow Scenarios:
  * BIZ-01: Maintenance Mode Stuck after update (disable_moodle_cli_maintenance_mode) -> ALLOW with approval IF upgrade=0.0
  * BIZ-02: Ad-hoc / Scheduled Task Queue Stuck (restart_moodle_cron_service on node A) -> ALLOW with approval (Infrastructure level)
- 5 Technical / Infrastructure Scenarios:
  * CON-01: Web Container Stopped (start_reviewed_compose_service) -> ALLOW with approval
  * DB-01: RDS Database Connectivity Lost (remove_scoped_db_reject) -> ALLOW with approval
  * RES-01: Node CPU Saturation (remove_named_cpu_load_container) -> ALLOW with approval
  * NET-01: Network / Dependency Resolution Drift (recreate_moodle_web_from_reviewed_compose) -> ALLOW with approval
  * SEC-01: Database Security Group Ingress Drift (remove_tagged_database_ingress_rule) -> DENY / HUMAN_ONLY
"""

from __future__ import annotations

import base64
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.evidence.adapters import SafeLiveActionRequest
from core.moodle.diagnosis import diagnose_moodle_alert
from core.moodle.planner import (
    BUSINESS_SCENARIO_REMEDIATION_DISPOSITION,
    SCENARIO_REMEDIATION_DISPOSITION,
    describe_moodle_remediation_disposition,
    plan_moodle_s3_3_remediation,
)
from core.safety.action_catalog import MOODLE_7_SCENARIO_IDS, ActionCatalog
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.safety.verifier_attestation import (
    VerifierAttestation,
    VerifierAttestationPayload,
    accept_verifier_attestation,
    canonical_payload,
)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment
from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS


ALLOWED_6_SCENARIOS = (
    "BIZ-01",
    "BIZ-02",
    "CON-01",
    "DB-01",
    "RES-01",
    "NET-01",
)
HUMAN_ONLY_1_SCENARIO = ("SEC-01",)


def _ed25519_keypair() -> tuple[bytes, bytes]:
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _metric(query_id: str, value: float, instance: str, suffix: str):
    return SimpleNamespace(
        source="prometheus",
        evidence_id=f"ev-{suffix}",
        collected_at=datetime.now(timezone.utc),
        metadata={
            "payload_json": json.dumps(
                {
                    "query_id": query_id,
                    "sample_value": value,
                    "labels": {"instance": instance},
                }
            )
        },
    )


def _scenario_evidence(scenario_id: str) -> tuple[str, list[SimpleNamespace]]:
    alerts_and_metrics = {
        # 2 Allowed Business/Infra Scenarios
        "BIZ-01": (
            "MoodleMaintenanceModeStuck",
            [
                ("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-a"),
                ("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-b"),
                ("moodle_upgrade_in_progress", 0.0, "moodle-app-a"),
                ("moodle_upgrade_in_progress", 0.0, "moodle-app-b"),
                ("moodle_node_web_running", 1.0, "moodle-app-a"),
                ("moodle_node_web_running", 1.0, "moodle-app-b"),
                ("moodle_public_probe", 1.0, "monitor-ai-01"),
            ],
        ),
        "BIZ-02": (
            "MoodleAdhocTaskQueueStuck",
            [
                ("moodle_adhoc_tasks_overdue_count", 5.0, "moodle-app-a"),
                ("moodle_adhoc_tasks_overdue_count", 5.0, "moodle-app-b"),
                ("moodle_cron_container_stopped", 1.0, "moodle-app-a"),
                ("moodle_cron_container_stopped", 0.0, "moodle-app-b"),
                ("moodle_node_web_running", 1.0, "moodle-app-a"),
                ("moodle_node_web_running", 1.0, "moodle-app-b"),
            ],
        ),
        # 4 Allowed Technical Scenarios
        "CON-01": (
            "MoodleWebContainerMissing",
            [
                ("moodle_fault_con01_web_stopped", 0.0, "moodle-app-a"),
                ("moodle_fault_con01_web_stopped", 1.0, "moodle-app-b"),
                ("moodle_node_web_running", 1.0, "moodle-app-a"),
                ("moodle_node_web_running", 0.0, "moodle-app-b"),
                ("moodle_node_up", 1.0, "moodle-app-a"),
                ("moodle_node_up", 1.0, "moodle-app-b"),
                ("moodle_cadvisor_up", 1.0, "moodle-app-a"),
                ("moodle_cadvisor_up", 1.0, "moodle-app-b"),
            ],
        ),
        "DB-01": (
            "MoodleNodeRdsTcpFailed",
            [
                ("moodle_node_rds_tcp_success", 0.0, "moodle-app-a"),
                ("moodle_node_rds_tcp_success", 0.0, "moodle-app-b"),
                ("moodle_fault_db01_tagged_rule", 1.0, "moodle-app-a"),
                ("moodle_fault_db01_tagged_rule", 1.0, "moodle-app-b"),
            ],
        ),
        "RES-01": (
            "MoodleNodeCpuHigh",
            [
                ("moodle_node_cpu_busy_percent", 20.0, "moodle-app-a"),
                ("moodle_node_cpu_busy_percent", 85.0, "moodle-app-b"),
                ("moodle_fault_res01_cpu_fixture_running", 0.0, "moodle-app-a"),
                ("moodle_fault_res01_cpu_fixture_running", 1.0, "moodle-app-b"),
                ("moodle_node_web_running", 1.0, "moodle-app-a"),
                ("moodle_node_web_running", 1.0, "moodle-app-b"),
            ],
        ),
        "NET-01": (
            "MoodleSyntheticTransactionFailed",
            [
                ("moodle_synthetic_success", 0.0, "monitor-ai-01"),
                ("moodle_rds_tcp_probe", 1.0, "monitor-ai-01"),
                ("moodle_public_probe", 1.0, "monitor-ai-01"),
                ("moodle_efs_mount", 1.0, "moodle-app-a"),
                ("moodle_efs_mount", 1.0, "moodle-app-b"),
                ("moodle_fault_net01_hosts_override", 1.0, "moodle-app-a"),
                ("moodle_fault_net01_hosts_override", 1.0, "moodle-app-b"),
            ],
        ),
    }
    alert_name, rows = alerts_and_metrics[scenario_id]
    evidence = [
        _metric(query_id, value, instance, f"{scenario_id}-{idx}")
        for idx, (query_id, value, instance) in enumerate(rows)
    ]
    return alert_name, evidence


def _get_disposition(scenario_id: str) -> dict[str, str]:
    return (
        BUSINESS_SCENARIO_REMEDIATION_DISPOSITION.get(scenario_id)
        or SCENARIO_REMEDIATION_DISPOSITION.get(scenario_id, {})
    )


def test_7_scenario_suite_defines_exact_2_business_and_5_technical_scenarios() -> None:
    assert MOODLE_7_SCENARIO_IDS == (
        "BIZ-01",
        "BIZ-02",
        "CON-01",
        "DB-01",
        "RES-01",
        "NET-01",
        "SEC-01",
    )
    biz_ids = [s for s in MOODLE_7_SCENARIO_IDS if s.startswith("BIZ-")]
    tech_ids = [s for s in MOODLE_7_SCENARIO_IDS if not s.startswith("BIZ-")]
    assert len(biz_ids) == 2
    assert len(tech_ids) == 5

    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    for scenario_id in MOODLE_7_SCENARIO_IDS:
        info = _get_disposition(scenario_id)
        assert info, f"Missing disposition for {scenario_id}"
        action_id = info["catalog_action_id"]
        entry = catalog.lookup(action_id)
        assert entry is not None, f"Missing catalog entry for {scenario_id}: {action_id}"


@pytest.mark.parametrize("scenario_id", ALLOWED_6_SCENARIOS)
def test_rq1_allowed_6_scenarios_diagnose_plan_and_pass_safety_gate_with_ed25519_approval(
    scenario_id: str,
) -> None:
    alert_name, evidence = _scenario_evidence(scenario_id)
    diagnosis = diagnose_moodle_alert(alert_name, evidence)
    assert diagnosis["status"] == "hypothesis"
    assert diagnosis["confidence"] >= 0.65

    # Planner must use all evidence refs from diagnosis
    diagnosis_for_plan = {
        **diagnosis,
        "evidence_refs": [item.evidence_id for item in evidence],
    }
    proposal = plan_moodle_s3_3_remediation(
        incident_id=f"inc-{scenario_id}",
        alert_name=alert_name,
        diagnosis=diagnosis_for_plan,
        evidence=evidence,
    )
    assert proposal is not None, f"Planning failed for {scenario_id}"
    assert proposal["scenario_id"] == scenario_id
    assert proposal["execution_permitted"] is False
    assert proposal["resolution_eligible"] is False

    request = SafeLiveActionRequest(
        request_id=f"req-{scenario_id}",
        idempotency_key=proposal["action"]["idempotency_key"],
        catalog_action_id=proposal["catalog_action_id"],
        scenario_id=proposal["scenario_id"],
        target_scope=proposal["target_scope"],
        action=TypedAction.model_validate(proposal["action"]),
    )

    priv_pem, pub_pem = _ed25519_keypair()
    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    gate = SafeExecutionGate(
        approval_verification_key=pub_pem,
        approved_actors={"safety-officer"},
        catalog=catalog,
        rollback_catalog=catalog,
    )

    # 1) Without human signature -> REQUIRE_APPROVAL
    verdict_unapproved = gate.evaluate(
        request, actor_role="executor", confidence=diagnosis["confidence"]
    )
    assert verdict_unapproved.decision == "REQUIRE_APPROVAL"

    # 2) With valid Ed25519 signature -> ALLOW
    approval = ApprovalRecord.create_ed25519(
        action_sha256=verdict_unapproved.action_sha256,
        actor_id="safety-officer",
        private_key_pem=priv_pem,
        ttl_seconds=600,
    )
    verdict_approved = gate.evaluate(
        request,
        actor_role="executor",
        confidence=diagnosis["confidence"],
        approval=approval,
    )
    assert verdict_approved.decision == "ALLOW"


def test_biz01_maintenance_mode_refuses_automated_disable_when_upgrade_in_progress() -> None:
    """When an upgrade is actively in progress, AI MUST NOT auto-disable maintenance mode."""
    evidence = [
        _metric("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-a", "b1-0"),
        _metric("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-b", "b1-1"),
        _metric("moodle_upgrade_in_progress", 1.0, "moodle-app-a", "b1-2"),  # Active upgrade!
        _metric("moodle_upgrade_in_progress", 0.0, "moodle-app-b", "b1-3"),
        _metric("moodle_node_web_running", 1.0, "moodle-app-a", "b1-4"),
        _metric("moodle_node_web_running", 1.0, "moodle-app-b", "b1-5"),
    ]
    diag = diagnose_moodle_alert("MoodleMaintenanceModeStuck", evidence)
    assert diag["status"] == "escalated_human_only"
    assert diag["cause_code"] == "upgrade_in_progress_human_escalation"

    # Plan MUST fail closed
    plan = plan_moodle_s3_3_remediation(
        incident_id="inc-biz01-upgrade-01",
        alert_name="MoodleMaintenanceModeStuck",
        diagnosis=diag,
        evidence=evidence,
    )
    assert plan is None, "AI must not plan maintenance disable while upgrade is in progress"


def test_biz02_cron_restarts_container_without_moodle_db_mutation() -> None:
    """BIZ-02 restarts moodle-cron container at infrastructure level, strictly without DB tables mutation."""
    alert_name, evidence = _scenario_evidence("BIZ-02")
    diag = diagnose_moodle_alert(alert_name, evidence)
    proposal = plan_moodle_s3_3_remediation(
        incident_id="inc-biz02-cron-01",
        alert_name=alert_name,
        diagnosis={**diag, "evidence_refs": [item.evidence_id for item in evidence]},
        evidence=evidence,
    )
    assert proposal is not None
    assert proposal["catalog_action_id"] == "restart_moodle_cron_service"
    assert proposal["action"]["action_type"] == ActionType.START_CONTAINER.value
    assert proposal["action"]["rollback_plan"]["rollback_action_id"] == "reconcile_biz02_cron_baseline"
    # Catalog entry check
    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    entry = catalog.lookup("restart_moodle_cron_service")
    assert entry.action_type == ActionType.START_CONTAINER
    assert entry.adapter == "docker"


def test_sec01_human_only_escalation_refuses_automated_execution() -> None:
    """SEC-01 (Database Security Group Ingress) MUST be refused at Safety Gate and escalated."""
    disposition = describe_moodle_remediation_disposition(
        alert_name="MoodleDatabasePortExposure",
        diagnosis={"status": "hypothesis", "cause_code": "rds_public_ingress_detected"},
        remediation_candidate=None,
    )
    assert disposition["disposition"] == "human_only_escalation"
    assert disposition["scenario_id"] == "SEC-01"
    assert disposition["execution_permitted"] is False

    priv_pem, pub_pem = _ed25519_keypair()
    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    gate = SafeExecutionGate(
        approval_verification_key=pub_pem,
        approved_actors={"safety-officer"},
        catalog=catalog,
        rollback_catalog=catalog,
    )
    request = SafeLiveActionRequest(
        request_id="req-sec01",
        idempotency_key="idem-sec01",
        catalog_action_id="remove_tagged_database_ingress_rule",
        scenario_id="SEC-01",
        target_scope="staging_database_security_group",
        action=TypedAction(
            action_id="act-sec01",
            incident_id="inc-sec01",
            action_type=ActionType.RUN_ANSIBLE_PLAYBOOK,
            target_resource_id="moodle-rds-security-group",
            environment=Environment.STAGING,
            reason="Attempted automated execution of SEC-01",
            expected_outcome="Attempted automated recovery of SEC-01",
            evidence_refs=["ev-1", "ev-2", "ev-3"],
            reversible=True,
            rollback_plan=RollbackPlan(
                available=True,
                rollback_action_id="remove_tagged_database_ingress_rule",
                method="Attempted fake rollback",
            ),
            requires_approval=True,
        ),
    )
    verdict = gate.evaluate(request, actor_role="executor", confidence=0.95)
    assert verdict.decision == "DENY"
    assert "SEC-01" not in MOODLE_LIVE_CONTRACTS


@pytest.mark.parametrize("scenario_id", ALLOWED_6_SCENARIOS)
def test_rq2_independent_verifier_resolves_all_6_allowed_scenarios(
    scenario_id: str,
) -> None:
    """All 6 allowed scenarios have live contracts and resolve via independent signed attestation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        priv_pem, pub_pem = _ed25519_keypair()
        pub_file = tmp_path / "verifier.pub"
        pub_file.write_bytes(pub_pem)
        audit_db = tmp_path / "safe_action_audit.sqlite3"

        incident_id = f"inc-{scenario_id}"
        request_id = f"req-{scenario_id}"
        info = _get_disposition(scenario_id)
        action_id = info["catalog_action_id"]
        target_scope = info["target_scope"]

        with SafeActionAuditStore(audit_db) as store:
            receipt_event = store.append(
                incident_id=incident_id,
                event_type="live_executor_result",
                payload={
                    "request_id": request_id,
                    "scenario_id": scenario_id,
                    "catalog_action_id": action_id,
                    "target_scope": target_scope,
                    "status": "executed",
                    "mutated": True,
                    "receipt_id": f"rcpt-{scenario_id}",
                },
            )
            store.append(
                incident_id=incident_id,
                event_type="live_verification_pending",
                payload={
                    "request_id": request_id,
                    "scenario_id": scenario_id,
                    "resolution_eligible": False,
                },
            )

        contract = MOODLE_LIVE_CONTRACTS[scenario_id]
        all_checks = {
            check: True
            for group in ("allowed", "forbidden", "related")
            for check in contract[group]
        }
        now = datetime.now(timezone.utc)
        samples = [
            {
                "observed_at": (now - timedelta(seconds=120 - 30 * idx)).isoformat(),
                "checks": all_checks,
            }
            for idx in range(5)
        ]
        observations = [
            StabilityObservation(observed_at=item["observed_at"], healthy=True, simulated=False)
            for item in samples
        ]
        verification = VerificationResult(
            verification_id=f"verify-{scenario_id}",
            incident_id=incident_id,
            health_passed=True,
            communication_contract_passed=True,
            stability_seconds=120,
            stability_observations=observations,
            resolution_eligible=True,
            allowed_probes=[
                ProbeResult(name=name, passed=True, simulated=False, details="5/5")
                for name in contract["allowed"]
            ],
            forbidden_probes=[
                ProbeResult(name=name, passed=True, simulated=False, details="5/5")
                for name in contract["forbidden"]
            ],
            related_probes=[
                ProbeResult(name=name, passed=True, simulated=False, details="5/5")
                for name in contract["related"]
            ],
            verdict="resolved",
            simulated=False,
            checked_at=now,
        )
        typed_payload = VerifierAttestationPayload.model_validate(
            {
                "version": 1,
                "incident_id": incident_id,
                "request_id": request_id,
                "scenario_id": scenario_id,
                "receipt_event_hash": receipt_event["event_hash"],
                "signed_at": now.isoformat(),
                "verification": verification.model_dump(mode="json"),
                "probe_samples": samples,
            }
        )
        priv_key = serialization.load_pem_private_key(priv_pem, password=None)
        signature = base64.b64encode(priv_key.sign(canonical_payload(typed_payload))).decode("ascii")
        attestation = VerifierAttestation(payload=typed_payload, signature=signature)

        result = accept_verifier_attestation(
            audit_db=audit_db,
            public_key_path=pub_file,
            request_id=request_id,
            attestation=attestation,
        )
        assert result["status"] == "resolved"
        assert result["idempotent"] is False
