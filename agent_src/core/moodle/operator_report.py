"""Administrator action report for a Moodle incident pipeline result.

The report separates what the agent already did (read-only collection,
diagnosis, proposal) from the steps an administrator must take. It only
describes the result; it never grants execution or resolution authority.
"""

from __future__ import annotations

from typing import Any

_MAX_TEXT_CHARS = 3500

AI_NEVER_DOES = (
    "Không tự thực thi thay đổi hạ tầng; mọi thay đổi cần 2 phê duyệt Ed25519 độc lập.",
    "Không sửa dữ liệu, cấu hình hay bảng mdl_* của ứng dụng Moodle.",
    "Không tự đánh dấu RESOLVED; chỉ Independent Verifier được làm việc này.",
)

# Manual, infrastructure-level guidance for diagnoses the agent must not act on.
_MANUAL_GUIDANCE: dict[str, tuple[str, ...]] = {
    "rds_ingress_unreviewed_source": (
        "Kỹ sư an ninh xác minh rule ingress lạ trên Security Group của RDS (nguồn, người tạo, thời điểm) qua CloudTrail.",
        "Gỡ đúng rule đó bằng thay đổi Terraform đã review (hoặc console nếu khẩn cấp, sau đó đồng bộ lại Terraform).",
        "Xác nhận moodle_rds_sg_unreviewed_ingress_rules trở về 0 và cảnh báo MoodleDatabasePortExposure tự hết.",
    ),
    "upgrade_in_progress_human_escalation": (
        "Không tắt chế độ bảo trì khi admin/cli/upgrade.php còn chạy.",
        "Kiểm tra tiến trình và log upgrade trong container Moodle; chờ upgrade hoàn tất hoặc xử lý lỗi upgrade.",
        "Khi moodle_upgrade_in_progress về 0, cảnh báo sẽ được chẩn đoán lại và có thể sinh đề xuất BIZ-01.",
    ),
    "rds_ingress_audit_unavailable": (
        "Telemetry SEC-01 đang mù: kiểm tra instance role read-only và tag AiopsRdsSecurityGroupId/AiopsRdsReviewedSourceGroups của monitor-ai-01.",
        "Chạy tay: systemctl start moodle-rds-ingress-audit.service rồi xem journalctl -u moodle-rds-ingress-audit.",
        "Trong lúc audit lỗi, tự kiểm tra Security Group của RDS trên AWS console.",
    ),
    "adhoc_queue_probe_failed": (
        "Telemetry BIZ-02 đang mù: chạy tay systemctl start moodle-node-probes.service trên node bị ảnh hưởng.",
        "Kiểm tra container web có nạp được Moodle CLI (config.php, kết nối DB) hay không.",
        "Trong lúc probe lỗi, kiểm tra trạng thái container moodle-cron bằng docker ps.",
    ),
}

_DISPOSITION_LABELS = {
    "candidate_proposed": "AI đã đề xuất hành động khắc phục, chờ admin phê duyệt",
    "human_only_escalation": "Chỉ con người được xử lý (HUMAN_ONLY)",
    "escalated_awaiting_operator_isolation": "Chuyển admin cô lập nguyên nhân",
    "awaiting_valid_evidence": "Chưa đủ bằng chứng hợp lệ để kết luận",
}


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "n/a"


def _ai_completed_steps(report: dict[str, Any]) -> list[str]:
    diagnosis = report.get("diagnosis") or {}
    steps: list[str] = []
    sources = ", ".join(report.get("evidence_sources") or []) or "không có"
    steps.append(
        f"Thu thập {len(report.get('evidence_refs') or [])} bằng chứng read-only "
        f"(nguồn: {sources}; trạng thái: {report.get('evidence_status', 'unknown')})."
    )
    if report.get("collector_errors"):
        steps.append(f"Ghi nhận {len(report['collector_errors'])} lỗi thu thập telemetry.")
    if diagnosis.get("status") == "hypothesis":
        steps.append(
            f"Chẩn đoán nguyên nhân: {diagnosis.get('hypothesis')} "
            f"[{diagnosis.get('cause_code')}, độ tin cậy {_percent(diagnosis.get('confidence'))}]."
        )
    else:
        steps.append(f"Chưa kết luận nguyên nhân: {diagnosis.get('reason') or report.get('reason') or 'không rõ'}.")
    confirmed = report.get("confirmed_affected_resources") or []
    potential = report.get("potentially_affected_resources") or []
    if confirmed or potential:
        steps.append(
            f"Đánh giá phạm vi: xác nhận {', '.join(confirmed) or 'không'}; "
            f"có thể ảnh hưởng {', '.join(potential) or 'không'}."
        )
    remediation = report.get("remediation_candidate") or {}
    plan = report.get("plan_candidate") or {}
    if remediation:
        steps.append(
            f"Đề xuất khắc phục {remediation.get('scenario_id')}: {remediation.get('catalog_action_id')} "
            f"trên {remediation.get('target_scope')} (có kế hoạch rollback, chưa thực thi)."
        )
    elif plan:
        steps.append(
            f"Đề xuất kiểm tra read-only: {(plan.get('parameters') or {}).get('catalog_action_id', plan.get('action_type'))} "
            f"trên {plan.get('target_resource_id')}."
        )
    if report.get("decision_evidence_refs"):
        steps.append(f"Lưu {len(report['decision_evidence_refs'])} hồ sơ quyết định có hash để audit.")
    return steps


def _admin_steps(report: dict[str, Any]) -> list[str]:
    if report.get("status") == "escalated":
        return [
            f"Xem lý do chuyển tiếp: {report.get('reason', 'không rõ')}.",
            "Xử lý thủ công theo runbook; agent không chạy chẩn đoán cho sự cố này.",
        ]
    if report.get("status") == "processing":
        return ["Chờ: một worker khác đang xử lý cùng sự cố này."]

    diagnosis = report.get("diagnosis") or {}
    disposition = (report.get("remediation_disposition") or {}).get("disposition")
    cause_code = str(diagnosis.get("cause_code") or "")
    remediation = report.get("remediation_candidate") or {}

    if remediation:
        candidate_id = report.get("remediation_candidate_evidence_id") or "<candidate_id>"
        rollback = ((remediation.get("action") or {}).get("rollback_plan") or {}).get("rollback_action_id")
        return [
            "Xem lại bằng chứng và chẩn đoán ở trên; chỉ tiếp tục nếu đúng cửa sổ bảo trì staging.",
            f"Chuẩn bị: POST /api/moodle/remediation/{candidate_id}/prepare (bearer token operator).",
            "Lấy 2 phê duyệt Ed25519 từ 2 người khác nhau (Safety Gate và Executor), ký ngoài host agent.",
            f"Thực thi: POST /api/moodle/remediation/{candidate_id}/execute kèm 2 phê duyệt.",
            "Chạy Independent Verifier; sự cố chỉ RESOLVED khi verifier ký xác nhận"
            + (f", nếu không đạt thì rollback bằng {rollback}." if rollback else "."),
        ]
    if cause_code in _MANUAL_GUIDANCE:
        return list(_MANUAL_GUIDANCE[cause_code])
    if disposition == "human_only_escalation":
        return [
            "Sự cố thuộc nhóm HUMAN_ONLY: admin tự xử lý theo runbook, agent không được thực thi.",
            "Sau khi sửa, xác nhận cảnh báo hết và chạy Independent Verifier.",
        ]
    if diagnosis.get("status") == "hypothesis":
        steps = [
            "Nguyên nhân chưa khớp đúng một fault contract đã duyệt; admin cô lập nguyên nhân thủ công.",
        ]
        plan = report.get("plan_candidate") or {}
        if plan:
            steps.append(
                f"Có thể chạy kiểm tra read-only "
                f"{(plan.get('parameters') or {}).get('catalog_action_id', plan.get('action_type'))} để có thêm bằng chứng."
            )
        steps.append("Tham khảo runbook đính kèm; mọi thay đổi vẫn phải qua Safety Gate.")
        return steps
    missing = diagnosis.get("missing_queries") or []
    steps = ["Kiểm tra telemetry trước khi xử lý: chẩn đoán bị chặn vì thiếu hoặc mâu thuẫn bằng chứng."]
    if missing:
        steps.append(f"Metric thiếu: {', '.join(missing)} (kiểm tra exporter/timer trên node).")
    if report.get("collector_errors"):
        steps.append("Kiểm tra kết nối agent tới Prometheus (PROMETHEUS_URL) và lỗi thu thập.")
    return steps


def build_admin_action_report(alert_name: str, report: dict[str, Any]) -> dict[str, Any]:
    """Summarize a pipeline result as AI-completed steps and administrator steps."""
    disposition = (report.get("remediation_disposition") or {}).get("disposition")
    headline = _DISPOSITION_LABELS.get(str(disposition), str(report.get("status") or "unknown"))
    ai_steps = _ai_completed_steps(report) if report.get("status") not in {"escalated", "processing"} else []
    admin_steps = _admin_steps(report)
    lines = [
        f"[Moodle AIOps] {alert_name} | incident={report.get('incident_id', 'unassigned')}",
        f"Kết luận: {headline}",
    ]
    if ai_steps:
        lines.append("AI đã thực hiện:")
        lines.extend(f"  {index}. {step}" for index, step in enumerate(ai_steps, 1))
    lines.append("Administrator cần thực hiện:")
    lines.extend(f"  {index}. {step}" for index, step in enumerate(admin_steps, 1))
    lines.append("AI không bao giờ: " + " ".join(AI_NEVER_DOES))
    text = "\n".join(lines)
    if len(text) > _MAX_TEXT_CHARS:
        text = text[: _MAX_TEXT_CHARS - 3] + "..."
    return {
        "alert_name": alert_name,
        "incident_id": report.get("incident_id"),
        "headline": headline,
        "ai_completed_steps": ai_steps,
        "admin_steps": admin_steps,
        "ai_never_does": list(AI_NEVER_DOES),
        "execution_permitted": False,
        "resolution_eligible": False,
        "text": text,
    }
