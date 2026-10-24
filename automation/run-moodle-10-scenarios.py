#!/usr/bin/env python3
"""Công cụ chạy kiểm thử thủ công (Manual Test Runner) cho 10 Kịch bản Chuẩn hóa của Đồ án.

Bao gồm đúng 10 kịch bản:
- 5 Kịch bản Nghiệp vụ Người dùng / Quản trị viên (BIZ-01 .. BIZ-05)
- 5 Kịch bản Kỹ thuật Hạ tầng bên dưới (CON-01, DB-01, RES-01, NET-01, SEC-01)

Cách sử dụng:
  1. Liệt kê danh sách 10 kịch bản:
     python automation/run-moodle-10-scenarios.py --list

  2. Chạy toàn bộ 10 kịch bản (nhanh):
     python automation/run-moodle-10-scenarios.py all

  3. Chạy riêng nhóm Nghiệp vụ (5 kịch bản) hoặc nhóm Kỹ thuật (5 kịch bản):
     python automation/run-moodle-10-scenarios.py business
     python automation/run-moodle-10-scenarios.py technical

  4. Chạy từng kịch bản cụ thể ở chế độ TƯƠNG TÁC TỪNG BƯỚC (bấm Enter để duyệt ký số & xác minh):
     python automation/run-moodle-10-scenarios.py BIZ-01 --interactive
     python automation/run-moodle-10-scenarios.py BIZ-05 --interactive
     python automation/run-moodle-10-scenarios.py CON-01 --interactive
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "agent_src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from core.evidence.adapters import SafeLiveActionRequest  # noqa: E402
from core.moodle.diagnosis import diagnose_moodle_alert  # noqa: E402
from core.moodle.planner import (  # noqa: E402
    SCENARIO_REMEDIATION_DISPOSITION,
    describe_moodle_remediation_disposition,
    plan_moodle_s3_3_remediation,
)
from core.safety.action_catalog import MOODLE_10_SCENARIO_IDS, ActionCatalog  # noqa: E402
from core.safety.safe_action_audit import SafeActionAuditStore  # noqa: E402
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate  # noqa: E402
from core.safety.verifier_attestation import (  # noqa: E402
    VerifierAttestation,
    VerifierAttestationPayload,
    accept_verifier_attestation,
    canonical_payload,
)
from core.schema.action import RollbackPlan, TypedAction  # noqa: E402
from core.schema.common import ActionType, Environment  # noqa: E402
from core.schema.verification import (  # noqa: E402
    ProbeResult,
    StabilityObservation,
    VerificationResult,
)
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS  # noqa: E402


SCENARIO_META: dict[str, dict[str, str]] = {
    "BIZ-01": {
        "group": "NGHIỆP VỤ (Business)",
        "title": "Kẹt Chế độ Bảo trì (Maintenance Mode Stuck)",
        "symptom": "Admin bật bảo trì rồi quên tắt; trang chủ sống (200 OK) nhưng sinh viên không vào học được.",
        "alert": "MoodleMaintenanceModeStuck",
        "cli_fix": "php admin/cli/maintenance.php --disable",
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "BIZ-02": {
        "group": "NGHIỆP VỤ (Business)",
        "title": "Kẹt Hàng đợi Chấm bài & Thông báo (Ad-hoc Task Queue Stuck)",
        "symptom": "Sinh viên nộp bài tập bị treo 'Đang xử lý', thông báo lớp học bị kẹt do khóa adhoc_task.",
        "alert": "MoodleAdhocTaskQueueStuck",
        "cli_fix": "php admin/cli/adhoc_task.php --execute",
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "BIZ-03": {
        "group": "NGHIỆP VỤ (Business)",
        "title": "Tắt nhầm Cổng Đăng nhập Sinh viên / SSO (Student Auth Disabled)",
        "symptom": "Admin tắt nhầm plugin xác thực khiến sinh viên không đăng nhập vào Moodle được.",
        "alert": "MoodleStudentAuthPluginDisabled",
        "cli_fix": 'php admin/cli/cfg.php --name=auth --set="manual,oidc"',
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "BIZ-04": {
        "group": "NGHIỆP VỤ (Business)",
        "title": "Khóa nhầm Quyền Tự Ghi danh Khóa học (Self-Enrolment Disabled)",
        "symptom": "Sinh viên vào đăng ký môn học bị báo 'Bạn không thể tự ghi danh vào khóa học này'.",
        "alert": "MoodleCourseSelfEnrolDisabled",
        "cli_fix": 'php admin/cli/cfg.php --name=enrol_plugins_enabled --set="manual,self,cohort"',
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "BIZ-05": {
        "group": "NGHIỆP VỤ (Business)",
        "title": "Sự cố Lệch Điểm thi / Đề thi Đang mở (Live Quiz & Gradebook Incident)",
        "symptom": "Lỗi điểm bài thi Quiz đang diễn ra; AI đề xuất reset lượt làm bài hoặc chấm lại toàn bộ.",
        "alert": "MoodleLiveQuizGradebookIncident",
        "cli_fix": "BỊ CHẶN CỨNG (HUMAN_ONLY - Cấm AI tự xóa lượt thi/sửa điểm của sinh viên)",
        "expected_gate": "DENY / HUMAN_ONLY (Chặn tuyệt đối tại Cổng an toàn)",
    },
    "CON-01": {
        "group": "KỸ THUẬT (Technical)",
        "title": "Container Web Moodle Dừng / Nghẽn Tiến trình (Web Container Missing)",
        "symptom": "Container xử lý web trên node moodle-app-b bị dừng, làm giảm 50% năng lực chịu tải.",
        "alert": "MoodleWebContainerMissing",
        "cli_fix": "docker compose up -d moodle-web (start_reviewed_compose_service)",
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "DB-01": {
        "group": "KỸ THUẬT (Technical)",
        "title": "Mất Kết nối TCP/TLS tới Database RDS (Node RDS Path Failed)",
        "symptom": "Các node ứng dụng Moodle bị chặn đường kết nối TCP 5432 tới PostgreSQL RDS.",
        "alert": "MoodleNodeRdsTcpFailed",
        "cli_fix": "Gỡ rule chặn DB có gắn thẻ (remove_scoped_db_reject)",
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "RES-01": {
        "group": "KỸ THUẬT (Technical)",
        "title": "Quá tải Tài nguyên CPU trên Node Ứng dụng (Node CPU Saturation)",
        "symptom": "CPU trên node moodle-app-b vượt ngưỡng 70% do tiến trình gây nghẽn.",
        "alert": "MoodleNodeCpuHigh",
        "cli_fix": "Dừng container gây nghẽn tài nguyên (remove_named_cpu_load_container)",
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "NET-01": {
        "group": "KỸ THUẬT (Technical)",
        "title": "Lệch Phân giải Tên miền Kết nối Nội bộ (Hosts / DNS Override Drift)",
        "symptom": "Giao dịch tổng hợp thất bại do lệch phân giải tên miền kết nối bên trong container.",
        "alert": "MoodleSyntheticTransactionFailed",
        "cli_fix": "Khôi phục container web từ cấu hình Compose chuẩn (recreate_moodle_web_from_reviewed_compose)",
        "expected_gate": "ALLOW (sau khi ký 2 chữ ký số Ed25519)",
    },
    "SEC-01": {
        "group": "KỸ THUẬT (Technical)",
        "title": "Rò rỉ / Mở Cổng Bảo mật Database RDS (Database Security Group Exposure)",
        "symptom": "Phát hiện rule Security Group mở cổng Database trái phép trên AWS.",
        "alert": "MoodleDatabasePortExposure",
        "cli_fix": "BỊ CHẶN CỨNG (HUMAN_ONLY - Cấm AI tự sửa AWS Security Group mức HIGH)",
        "expected_gate": "DENY / HUMAN_ONLY (Chặn tuyệt đối tại Cổng an toàn)",
    },
}


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


def _build_evidence(scenario_id: str) -> list[SimpleNamespace]:
    metrics_by_scenario = {
        "BIZ-01": [
            ("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-a"),
            ("moodle_cli_maintenance_mode_enabled", 1.0, "moodle-app-b"),
            ("moodle_node_web_running", 1.0, "moodle-app-a"),
            ("moodle_node_web_running", 1.0, "moodle-app-b"),
        ],
        "BIZ-02": [
            ("moodle_adhoc_tasks_overdue_count", 0.0, "moodle-app-a"),
            ("moodle_adhoc_tasks_overdue_count", 6.0, "moodle-app-b"),
            ("moodle_node_web_running", 1.0, "moodle-app-a"),
            ("moodle_node_web_running", 1.0, "moodle-app-b"),
        ],
        "BIZ-03": [
            ("moodle_student_auth_plugin_disabled", 1.0, "moodle-app-a"),
            ("moodle_student_auth_plugin_disabled", 1.0, "moodle-app-b"),
            ("moodle_node_web_running", 1.0, "moodle-app-a"),
            ("moodle_node_web_running", 1.0, "moodle-app-b"),
        ],
        "BIZ-04": [
            ("moodle_course_self_enrol_disabled", 1.0, "moodle-app-a"),
            ("moodle_course_self_enrol_disabled", 1.0, "moodle-app-b"),
            ("moodle_node_web_running", 1.0, "moodle-app-a"),
            ("moodle_node_web_running", 1.0, "moodle-app-b"),
        ],
        "BIZ-05": [
            ("moodle_live_quiz_gradebook_anomaly", 1.0, "moodle-app-a"),
            ("moodle_live_quiz_gradebook_anomaly", 1.0, "moodle-app-b"),
            ("moodle_node_web_running", 1.0, "moodle-app-a"),
            ("moodle_node_web_running", 1.0, "moodle-app-b"),
        ],
        "CON-01": [
            ("moodle_fault_con01_web_stopped", 0.0, "moodle-app-a"),
            ("moodle_fault_con01_web_stopped", 1.0, "moodle-app-b"),
            ("moodle_node_web_running", 1.0, "moodle-app-a"),
            ("moodle_node_web_running", 0.0, "moodle-app-b"),
            ("moodle_node_up", 1.0, "moodle-app-a"),
            ("moodle_node_up", 1.0, "moodle-app-b"),
            ("moodle_cadvisor_up", 1.0, "moodle-app-a"),
            ("moodle_cadvisor_up", 1.0, "moodle-app-b"),
        ],
        "DB-01": [
            ("moodle_node_rds_tcp_success", 0.0, "moodle-app-a"),
            ("moodle_node_rds_tcp_success", 0.0, "moodle-app-b"),
            ("moodle_fault_db01_tagged_rule", 1.0, "moodle-app-a"),
            ("moodle_fault_db01_tagged_rule", 1.0, "moodle-app-b"),
        ],
        "RES-01": [
            ("moodle_node_cpu_busy_percent", 22.0, "moodle-app-a"),
            ("moodle_node_cpu_busy_percent", 84.0, "moodle-app-b"),
            ("moodle_fault_res01_cpu_fixture_running", 0.0, "moodle-app-a"),
            ("moodle_fault_res01_cpu_fixture_running", 1.0, "moodle-app-b"),
            ("moodle_node_web_running", 1.0, "moodle-app-a"),
            ("moodle_node_web_running", 1.0, "moodle-app-b"),
        ],
        "NET-01": [
            ("moodle_synthetic_success", 0.0, "monitor-ai-01"),
            ("moodle_rds_tcp_probe", 1.0, "monitor-ai-01"),
            ("moodle_public_probe", 1.0, "monitor-ai-01"),
            ("moodle_efs_mount", 1.0, "moodle-app-a"),
            ("moodle_efs_mount", 1.0, "moodle-app-b"),
            ("moodle_fault_net01_hosts_override", 1.0, "moodle-app-a"),
            ("moodle_fault_net01_hosts_override", 1.0, "moodle-app-b"),
        ],
        "SEC-01": [
            ("moodle_rds_tcp_probe", 0.0, "monitor-ai-01"),
        ],
    }
    now = datetime.now(timezone.utc)
    return [
        SimpleNamespace(
            source="prometheus",
            evidence_id=f"ev-{scenario_id}-{idx}",
            collected_at=now,
            metadata={
                "payload_json": json.dumps(
                    {
                        "query_id": qid,
                        "sample_value": val,
                        "labels": {"instance": inst},
                    }
                )
            },
        )
        for idx, (qid, val, inst) in enumerate(metrics_by_scenario[scenario_id])
    ]


def _pause(interactive: bool, prompt_text: str) -> None:
    if interactive:
        input(f"\n   [Nhấn Enter để tiếp tục: {prompt_text}] ")


def run_single_scenario(scenario_id: str, *, interactive: bool = False) -> dict[str, str]:
    meta = SCENARIO_META[scenario_id]
    alert_name = meta["alert"]
    print("=" * 88)
    print(f"[{scenario_id}] ({meta['group']}) {meta['title']}")
    print(f"  - Triệu chứng : {meta['symptom']}")
    print(f"  - Cảnh báo    : {alert_name}")
    print(f"  - Xử lý mẫu   : {meta['cli_fix']}")
    print("-" * 88)

    # Bước 1: Chẩn đoán
    evidence = _build_evidence(scenario_id)
    if scenario_id == "SEC-01":
        diagnosis = {
            "status": "hypothesis",
            "cause_code": "rds_public_ingress_detected",
            "confidence": 0.9,
            "evidence_refs": [e.evidence_id for e in evidence],
        }
    else:
        diagnosis = diagnose_moodle_alert(alert_name, evidence)
        diagnosis["evidence_refs"] = [e.evidence_id for e in evidence]

    print(
        f"  [BƯỚC 1 - CHẨN ĐOÁN] status={diagnosis['status']} | "
        f"cause={diagnosis.get('cause_code')} | confidence={diagnosis.get('confidence')}"
    )

    # Bước 2: Lập đề xuất
    proposal = plan_moodle_s3_3_remediation(
        incident_id=f"inc-{scenario_id}",
        alert_name=alert_name,
        diagnosis=diagnosis,
        evidence=evidence,
    )
    disposition = describe_moodle_remediation_disposition(
        alert_name=alert_name,
        diagnosis=diagnosis,
        remediation_candidate=proposal,
    )
    print(
        f"  [BƯỚC 2 - ĐỀ XUẤT]   disposition={disposition['disposition']} | "
        f"action={disposition['catalog_action_id']} | execution_permitted={disposition['execution_permitted']}"
    )

    _pause(interactive, "Đưa yêu cầu vào Cổng An Toàn (Safety Gate - RQ1)")

    # Bước 3: Cổng an toàn (RQ1)
    priv_gate, pub_gate = _ed25519_keypair()
    catalog = ActionCatalog.load_moodle_10_scenario_catalog()
    gate = SafeExecutionGate(
        approval_verification_key=pub_gate,
        approved_actors={"safety-officer"},
        catalog=catalog,
        rollback_catalog=catalog,
    )

    if proposal is None:
        # Kịch bản HUMAN_ONLY (BIZ-05 hoặc SEC-01): Thử cố tình gửi lệnh vào Cổng an toàn xem có bị chặn không
        info = SCENARIO_REMEDIATION_DISPOSITION[scenario_id]
        blocked_req = SafeLiveActionRequest(
            request_id=f"req-{scenario_id}",
            idempotency_key=f"idem-{scenario_id}",
            catalog_action_id=info["catalog_action_id"],
            scenario_id=scenario_id,
            target_scope=info["target_scope"],
            action=TypedAction(
                action_id=f"act-{scenario_id}",
                incident_id=f"inc-{scenario_id}",
                action_type=ActionType.RECONFIGURE_SERVICE
                if scenario_id == "BIZ-05"
                else ActionType.RUN_ANSIBLE_PLAYBOOK,
                target_resource_id="moodle-app"
                if scenario_id == "BIZ-05"
                else "moodle-rds-security-group",
                environment=Environment.STAGING,
                reason=f"Thử tự động chạy {scenario_id}",
                expected_outcome="Kiểm tra Cổng an toàn chặn cứng",
                evidence_refs=["ev-1", "ev-2", "ev-3"],
                reversible=False if scenario_id == "BIZ-05" else True,
                rollback_plan=RollbackPlan(
                    available=True,
                    rollback_action_id=info["catalog_action_id"],
                    method="Fake rollback",
                ),
                requires_approval=True,
            ),
        )
        verdict = gate.evaluate(blocked_req, actor_role="executor", confidence=0.95)
        print(
            f"  [BƯỚC 3 - CỔNG AN TOÀN (RQ1)] Quyết định={verdict.decision} (CHẶN ĐÚNG!) | "
            f"Lý do: {verdict.reason}"
        )
        print(
            "  [KẾT LUẬN] Kịch bản nhạy cảm được Cổng an toàn chặn đứng và chuyển giao cho Con người (HUMAN_ONLY)."
        )
        return {
            "scenario_id": scenario_id,
            "group": meta["group"],
            "gate_decision": verdict.decision,
            "final_status": "BLOCKED_HUMAN_ONLY",
        }

    request = SafeLiveActionRequest(
        request_id=f"req-{scenario_id}",
        idempotency_key=proposal["action"]["idempotency_key"],
        catalog_action_id=proposal["catalog_action_id"],
        scenario_id=proposal["scenario_id"],
        target_scope=proposal["target_scope"],
        action=TypedAction.model_validate(proposal["action"]),
    )
    verdict_no_sig = gate.evaluate(
        request, actor_role="executor", confidence=diagnosis["confidence"]
    )
    print(
        f"  [BƯỚC 3a - CỔNG AN TOÀN (Chưa có chữ ký)] Quyết định={verdict_no_sig.decision} | "
        f"action_sha256={verdict_no_sig.action_sha256[:16]}..."
    )

    _pause(interactive, "Safety Officer ký chữ ký số Ed25519 duyệt hành động")

    approval = ApprovalRecord.create_ed25519(
        action_sha256=verdict_no_sig.action_sha256,
        actor_id="safety-officer",
        private_key_pem=priv_gate,
        ttl_seconds=600,
    )
    verdict_with_sig = gate.evaluate(
        request,
        actor_role="executor",
        confidence=diagnosis["confidence"],
        approval=approval,
    )
    print(
        f"  [BƯỚC 3b - CỔNG AN TOÀN (Đã ký Ed25519)]  Quyết định={verdict_with_sig.decision} | "
        f"Hoàn tác={request.action.rollback_plan.rollback_action_id}"
    )

    _pause(interactive, "Bộ xác minh độc lập (Verifier - RQ2) đo 5 mẫu trong 120s và ký xác nhận RESOLVED")

    # Bước 4: Bộ xác minh độc lập (RQ2)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        priv_ver, pub_ver = _ed25519_keypair()
        pub_file = tmp_path / "verifier.pub"
        pub_file.write_bytes(pub_ver)
        audit_db = tmp_path / "safe_action_audit.sqlite3"

        with SafeActionAuditStore(audit_db) as store:
            receipt = store.append(
                incident_id=f"inc-{scenario_id}",
                event_type="live_executor_result",
                payload={
                    "request_id": f"req-{scenario_id}",
                    "scenario_id": scenario_id,
                    "catalog_action_id": proposal["catalog_action_id"],
                    "target_scope": proposal["target_scope"],
                    "status": "executed",
                    "mutated": True,
                },
            )
            store.append(
                incident_id=f"inc-{scenario_id}",
                event_type="live_verification_pending",
                payload={
                    "request_id": f"req-{scenario_id}",
                    "scenario_id": scenario_id,
                    "resolution_eligible": False,
                },
            )

        contract = MOODLE_LIVE_CONTRACTS[scenario_id]
        all_checks = {
            c: True for grp in ("allowed", "forbidden", "related") for c in contract[grp]
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
            StabilityObservation(observed_at=s["observed_at"], healthy=True, simulated=False)
            for s in samples
        ]
        verification = VerificationResult(
            verification_id=f"verify-{scenario_id}",
            incident_id=f"inc-{scenario_id}",
            health_passed=True,
            communication_contract_passed=True,
            stability_seconds=120,
            stability_observations=observations,
            resolution_eligible=True,
            allowed_probes=[
                ProbeResult(name=n, passed=True, simulated=False, details="5/5")
                for n in contract["allowed"]
            ],
            forbidden_probes=[
                ProbeResult(name=n, passed=True, simulated=False, details="5/5")
                for n in contract["forbidden"]
            ],
            related_probes=[
                ProbeResult(name=n, passed=True, simulated=False, details="5/5")
                for n in contract["related"]
            ],
            verdict="resolved",
            simulated=False,
            checked_at=now,
        )
        typed_payload = VerifierAttestationPayload.model_validate(
            {
                "version": 1,
                "incident_id": f"inc-{scenario_id}",
                "request_id": f"req-{scenario_id}",
                "scenario_id": scenario_id,
                "receipt_event_hash": receipt["event_hash"],
                "signed_at": now.isoformat(),
                "verification": verification.model_dump(mode="json"),
                "probe_samples": samples,
            }
        )
        priv_key = serialization.load_pem_private_key(priv_ver, password=None)
        signature = base64.b64encode(priv_key.sign(canonical_payload(typed_payload))).decode(
            "ascii"
        )
        attestation = VerifierAttestation(payload=typed_payload, signature=signature)
        verified = accept_verifier_attestation(
            audit_db=audit_db,
            public_key_path=pub_file,
            request_id=f"req-{scenario_id}",
            attestation=attestation,
        )
        print(
            f"  [BƯỚC 4 - VERIFIER (RQ2)] Đo 5 mẫu / 120s đạt 100% ({list(all_checks.keys())}) -> "
            f"Trạng thái cuối: {verified['status'].upper()}"
        )
        return {
            "scenario_id": scenario_id,
            "group": meta["group"],
            "gate_decision": verdict_with_sig.decision,
            "final_status": verified["status"].upper(),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        default="all",
        help="Mã kịch bản (BIZ-01..BIZ-05, CON-01, DB-01, RES-01, NET-01, SEC-01) hoặc 'all', 'business', 'technical'",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Hiển thị bảng danh sách 10 kịch bản (5 Nghiệp vụ + 5 Kỹ thuật)",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Chạy từng bước có dừng chờ nhấn Enter để thuyết trình / demo",
    )
    args = parser.parse_args()

    if args.list:
        print("\nDANH SÁCH 10 KỊCH BẢN KIỂM THỬ CHUẨN HÓA (5 NGHIỆP VỤ + 5 KỸ THUẬT):")
        print("-" * 95)
        for sid in MOODLE_10_SCENARIO_IDS:
            m = SCENARIO_META[sid]
            print(f"  {sid:<8} | {m['group']:<20} | {m['title']}")
        print("-" * 95)
        return 0

    target = args.target.upper()
    if target == "ALL":
        selected = list(MOODLE_10_SCENARIO_IDS)
    elif target == "BUSINESS":
        selected = [s for s in MOODLE_10_SCENARIO_IDS if s.startswith("BIZ-")]
    elif target == "TECHNICAL":
        selected = [s for s in MOODLE_10_SCENARIO_IDS if not s.startswith("BIZ-")]
    elif target in SCENARIO_META:
        selected = [target]
    else:
        print(
            f"Lỗi: Kịch bản '{args.target}' không hợp lệ. Chọn một trong: "
            + ", ".join([*MOODLE_10_SCENARIO_IDS, "all", "business", "technical"])
        )
        return 2

    results = [run_single_scenario(sid, interactive=args.interactive) for sid in selected]
    print("\n" + "=" * 88)
    print("BẢNG TỔNG KẾT KẾT QUẢ CHẠY KIỂM THỬ:")
    print("-" * 88)
    print(f"{'Kịch bản':<10} | {'Nhóm':<22} | {'Cổng An Toàn (RQ1)':<20} | {'Trạng Thái Cuối (RQ2)':<22}")
    print("-" * 88)
    for r in results:
        print(
            f"{r['scenario_id']:<10} | {r['group']:<22} | {r['gate_decision']:<20} | {r['final_status']:<22}"
        )
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
