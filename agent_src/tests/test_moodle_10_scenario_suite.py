"""End-to-end test suite for the 10-Scenario Moodle Benchmark (5 Business + 5 Technical).

Validates both Research Questions across the simplified 10-scenario matrix:
- 5 Business / User-Admin Workflow Scenarios:
  * BIZ-01: Maintenance Mode Stuck (disable_moodle_cli_maintenance_mode) -> ALLOW with approval
  * BIZ-02: Ad-hoc / Scheduled Task Queue Stuck (release_stuck_moodle_adhoc_task_lock) -> ALLOW with approval
  * BIZ-03: Student Auth Plugin Disabled (restore_approved_moodle_auth_plugins) -> ALLOW with approval
  * BIZ-04: Course Self-Enrolment Disabled (restore_approved_course_self_enrolment) -> ALLOW with approval
  * BIZ-05: Live Quiz & Gradebook Incident (reset_live_quiz_attempts_and_regrade) -> DENY / HUMAN_ONLY
- 5 Technical / Infrastructure Scenarios:
  * CON-01: Web Container Stopped / Worker Pool Exhaustion (start_reviewed_compose_service) -> ALLOW with approval
  * DB-01: RDS Database Connectivity / TLS Mismatch (remove_scoped_db_reject) -> ALLOW with approval
  * RES-01: Node Resource / Local Cache Saturation (remove_named_cpu_load_container) -> ALLOW with approval
  * NET-01: Network / Dependency Resolution Drift (recreate_moodle_web_from_reviewed_compose) -> ALLOW with approval
  * SEC-01: Database Security Group / Secret Compromise (remove_tagged_database_ingress_rule) -> DENY / HUMAN_ONLY
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
    SCENARIO_REMEDIATION_DISPOSITION,
    describe_moodle_remediation_disposition,
    plan_moodle_s3_3_remediation,
)
from core.safety.action_catalog import MOODLE_10_SCENARIO_IDS, ActionCatalog
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


ALLOWED_8_SCENARIOS = (
    "BIZ-01",
    "BIZ-02",
    "BIZ-03",
    "BIZ-04",
    "CON-01",
    "DB-01",
    "RES-01",
    "NET-01",
)
HUMAN_ONLY_2_SCENARIOS = ("BIZ-05", "SEC-01")


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
        # 4 Allowed Business Scenarios
        "BIZ-01": (
            "MoodleMaintenanceModeStuck",
            [
                ("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-a"),
                ("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-b"),
                ("moodle_node_web_running", 1.0, "moodle-app-a"),
                ("moodle_node_web_running", 1.0, "moodle-app-b"),
            ],
        ),
        "BIZ-02": (
            "MoodleAdhocTaskQueueStuck",
            [
                ("moodle_adhoc_tasks_overdue_count", 0.0, "moodle-app-a"),
                ("moodle_adhoc_tasks_overdue_count", 5.0, "moodle-app-b"),
                ("moodle_node_web_running", 1.0, "moodle-app-a"),
                ("moodle_node_web_running", 1.0, "moodle-app-b"),
            ],
        ),
        "BIZ-03": (
            "MoodleStudentAuthPluginDisabled",
            [
                ("moodle_student_auth_plugin_disabled", 1.0, "moodle-app-a"),
                ("moodle_student_auth_plugin_disabled", 1.0, "moodle-app-b"),
                ("moodle_node_web_running", 1.0, "moodle-app-a"),
                ("moodle_node_web_running", 1.0, "moodle-app-b"),
            ],
        ),
        "BIZ-04": (
            "MoodleCourseSelfEnrolDisabled",
            [
                ("moodle_course_self_enrol_disabled", 1.0, "moodle-app-a"),
                ("moodle_course_self_enrol_disabled", 1.0, "moodle-app-b"),
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


def test_10_scenario_suite_defines_exact_5_business_and_5_technical_scenarios() -> None:
    assert MOODLE_10_SCENARIO_IDS == (
        "BIZ-01",
        "BIZ-02",
        "BIZ-03",
        "BIZ-04",
        "BIZ-05",
        "CON-01",
        "DB-01",
        "RES-01",
        "NET-01",
        "SEC-01",
    )
    biz_ids = [s for s in MOODLE_10_SCENARIO_IDS if s.startswith("BIZ-")]
    tech_ids = [s for s in MOODLE_10_SCENARIO_IDS if not s.startswith("BIZ-")]
    assert len(biz_ids) == 5
    assert len(tech_ids) == 5

    catalog = ActionCatalog.load_moodle_10_scenario_catalog()
    for scenario_id in MOODLE_10_SCENARIO_IDS:
        assert scenario_id in SCENARIO_REMEDIATION_DISPOSITION
        action_id = SCENARIO_REMEDIATION_DISPOSITION[scenario_id]["catalog_action_id"]
        entry = catalog.lookup(action_id)
        assert entry is not None, f"Missing catalog entry for {scenario_id}: {action_id}"


@pytest.mark.parametrize("scenario_id", ALLOWED_8_SCENARIOS)
def test_rq1_allowed_8_scenarios_diagnose_plan_and_pass_safety_gate_with_ed25519_approval(
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
    assert proposal is not None
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
    catalog = ActionCatalog.load_moodle_10_scenario_catalog()
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


@pytest.mark.parametrize(
    ("scenario_id", "alert_name", "action_id", "target_resource", "target_scope"),
    [
        (
            "BIZ-05",
            "MoodleLiveQuizGradebookIncident",
            "reset_live_quiz_attempts_and_regrade",
            "moodle-app",
            "staging_moodle_app",
        ),
        (
            "SEC-01",
            "MoodleDatabasePortExposure",
            "remove_tagged_database_ingress_rule",
            "moodle-rds-security-group",
            "staging_database_security_group",
        ),
    ],
)
def test_rq1_human_only_business_and_technical_scenarios_are_strictly_blocked_by_safety_gate(
    scenario_id: str,
    alert_name: str,
    action_id: str,
    target_resource: str,
    target_scope: str,
) -> None:
    disposition = describe_moodle_remediation_disposition(
        alert_name=alert_name,
        diagnosis={
            "status": "hypothesis",
            "cause_code": "moodle_live_quiz_gradebook_incident"
            if scenario_id == "BIZ-05"
            else "rds_public_ingress_detected",
        },
        remediation_candidate=None,
    )
    assert disposition["disposition"] == "human_only_escalation"
    assert disposition["scenario_id"] == scenario_id
    assert disposition["execution_permitted"] is False

    priv_pem, pub_pem = _ed25519_keypair()
    catalog = ActionCatalog.load_moodle_10_scenario_catalog()
    gate = SafeExecutionGate(
        approval_verification_key=pub_pem,
        approved_actors={"safety-officer"},
        catalog=catalog,
        rollback_catalog=catalog,
    )
    request = SafeLiveActionRequest(
        request_id=f"req-{scenario_id}",
        idempotency_key=f"idem-{scenario_id}",
        catalog_action_id=action_id,
        scenario_id=scenario_id,
        target_scope=target_scope,
        action=TypedAction(
            action_id=f"act-{scenario_id}",
            incident_id=f"inc-{scenario_id}",
            action_type=ActionType.RECONFIGURE_SERVICE
            if scenario_id == "BIZ-05"
            else ActionType.RUN_ANSIBLE_PLAYBOOK,
            target_resource_id=target_resource,
            environment=Environment.STAGING,
            reason=f"Attempted automated execution of {scenario_id}",
            expected_outcome=f"Attempted automated recovery of {scenario_id}",
            evidence_refs=["ev-1", "ev-2", "ev-3"],
            reversible=False if scenario_id == "BIZ-05" else True,
            rollback_plan=RollbackPlan(
                available=True,
                rollback_action_id=action_id,
                method="Attempted fake rollback",
            ),
            requires_approval=True,
        ),
    )
    verdict = gate.evaluate(request, actor_role="executor", confidence=0.95)
    assert verdict.decision == "DENY"
    assert scenario_id not in MOODLE_LIVE_CONTRACTS


@pytest.mark.parametrize("scenario_id", ALLOWED_8_SCENARIOS)
def test_rq2_independent_verifier_resolves_all_8_allowed_scenarios_only_after_5_samples_120s(
    scenario_id: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        priv_pem, pub_pem = _ed25519_keypair()
        pub_file = tmp_path / "verifier.pub"
        pub_file.write_bytes(pub_pem)
        audit_db = tmp_path / "safe_action_audit.sqlite3"

        incident_id = f"inc-{scenario_id}"
        request_id = f"req-{scenario_id}"
        action_id = SCENARIO_REMEDIATION_DISPOSITION[scenario_id]["catalog_action_id"]
        target_scope = SCENARIO_REMEDIATION_DISPOSITION[scenario_id]["target_scope"]

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

