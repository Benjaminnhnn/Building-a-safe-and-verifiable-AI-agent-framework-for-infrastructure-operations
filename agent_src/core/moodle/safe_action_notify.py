"""Telegram summaries for the Moodle incident workflow.

Step 2/3 (alert and diagnosis) is sent by the Celery worker; steps 4 (Safety
Gate prepare), 5 (executor dispatch) and 6 (Independent Verifier acceptance)
by the Agent API. The approval prompt is sent by the operator-side bridge
(automation/moodle-telegram-approval.py), which holds the signing keys; the
Agent never signs.

Messages carry identifiers, decisions and short hash prefixes only: never
approval signatures, tokens or request bodies. Notification is best effort and
must never change a safety decision.
"""

from __future__ import annotations

import ast
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_GATE_CHECK_LABELS = (
    "Cấu trúc yêu cầu hợp lệ",
    "Kill switch đang tắt",
    "Có ít nhất 3 bằng chứng khác nhau",
    "Hành động và thời gian thực thi nằm trong catalog",
    "Có rollback đối ứng",
    "Đúng vai trò, phạm vi và môi trường",
    "Mục tiêu và loại tài nguyên được phép",
    "Độ tin cậy chẩn đoán đạt ngưỡng",
)

_REMEDIATION_TEXT = {
    "BIZ-01": "Tắt chế độ bảo trì CLI sau khi xác nhận Moodle không đang nâng cấp.",
    "BIZ-02": "Khởi động lại Moodle cron trên node A; giữ nguyên dữ liệu hàng đợi.",
    "CON-01": "Khởi động lại Moodle web trên node B bằng Compose đã duyệt.",
    "DB-01": "Gỡ đúng rule chặn TCP 5432 tới RDS trên hai node Moodle.",
    "RES-01": "Dừng container tải CPU được gắn nhãn lỗi trên node B.",
    "NET-01": "Tạo lại web container từ Compose để bỏ bản ghi /etc/hosts sai.",
}

_HUMAN_TEXT = {
    "SEC-01": "Kỹ sư an ninh kiểm tra và thu hồi đúng rule ingress lạ của RDS.",
}


# Reviewed, static next-step hints per Safety Gate rule (see SafeExecutionGate.diagnose).
# They describe what would have to change; they never relax the rule.
_RULE_HINTS = {
    "G00": "Yêu cầu sai cấu trúc: chuẩn bị lại từ candidate mới, không sửa tay request.",
    "G01": "Kill switch đang bật: ngoài thẩm quyền Operator, liên hệ chủ hệ thống.",
    "G02": "Độ tin cậy không hợp lệ: chạy lại chẩn đoán với bằng chứng mới.",
    "G03": "Yêu cầu này đã từng qua Gate: chờ alert mới hoặc gọi lại /prepare cho candidate mới.",
    "G04": "Thiếu bằng chứng: chạy probe read-only của kịch bản để bổ sung, rồi /prepare lại.",
    "G05": "Executor không nhận tham số tự do: chọn đúng hành động trong catalog.",
    "G06": "Hành động không có trong catalog hoặc timeout vượt giới hạn: cần review catalog, không tự thêm.",
    "G07": "Rollback trong catalog không đối ứng: cần review cặp hành động/rollback.",
    "G08": "Khả năng hoàn tác khác catalog: tạo lại request từ planner.",
    "G09": "Thiếu kế hoạch rollback đúng catalog: tạo lại request từ planner.",
    "G10": "Loại hành động khác catalog: tạo lại request từ planner.",
    "G11": "Ngoài kịch bản/phạm vi/môi trường/vai trò đã duyệt: xử lý tay theo runbook hoặc review catalog.",
    "G12": "Đích nằm ngoài phạm vi đã duyệt: kiểm tra lại node bị ảnh hưởng; không mở rộng phạm vi để vượt Gate.",
    "G13": "Loại tài nguyên không hợp lệ cho hành động: chọn hành động khác trong catalog.",
    "G14": "Chẩn đoán chưa chắc (< 0.65): thu thêm bằng chứng phân biệt rồi /prepare lại.",
}
_MAX_DENY_RULES = 5


def _deny_card(preview: dict[str, Any]) -> list[str]:
    """List every failing Gate rule, split into hard and remediable blocks."""
    rules = [
        item for item in (preview.get("failed_rules") or [])
        if isinstance(item, dict) and item.get("status") == "fail"
    ]
    if not rules:
        return []
    lines = [f"Luật không đạt ({len(rules)}):"]
    for item in rules[:_MAX_DENY_RULES]:
        rule_id = str(item.get("rule_id") or "?")
        kind = "CỨNG" if item.get("class") == "HARD" else "KHẮC PHỤC ĐƯỢC"
        observed = _short(item.get("observed"), 60)
        required = _short(item.get("required"), 60)
        lines.append(f"• {rule_id} [{kind}] thấy {observed}, cần {required}")
        lines.append(f"  → {_RULE_HINTS.get(rule_id, 'Kiểm tra theo runbook.')}")
    if len(rules) > _MAX_DENY_RULES:
        lines.append(f"… và {len(rules) - _MAX_DENY_RULES} luật khác (xem gate_preview).")
    if any(item.get("class") == "HARD" for item in rules):
        lines.append("Có luật CỨNG: yêu cầu này không thể đạt bằng bằng chứng mới; cần hành động khác hoặc xử lý tay.")
    else:
        lines.append("Chỉ có luật KHẮC PHỤC ĐƯỢC: bổ sung đúng các điều kiện trên rồi /prepare lại.")
    return lines


def _short(value: Any, length: int = 12) -> str:
    text = str(value or "")
    return f"{text[:length]}…" if len(text) > length else (text or "none")


def _as_dict(value: Any) -> dict[str, Any]:
    """Reports may carry nested dicts or their Python repr; accept both."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.startswith("{"):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def format_alert_report(
    alert: dict[str, Any], report: dict[str, Any], admin_steps: list[str] | None = None
) -> str:
    """Short operator-facing alert; the detailed audit remains in evidence.

    ``admin_steps`` (from operator_report) are shown only when there is no
    candidate, so a blocked or human-only incident still tells the operator
    what to do next instead of only a generic line.
    """
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    diagnosis = _as_dict(report.get("diagnosis"))
    disposition = _as_dict(report.get("remediation_disposition"))
    candidate = _as_dict(report.get("remediation_candidate"))
    scenario = (
        disposition.get("scenario_id") or candidate.get("scenario_id") or report.get("scenario_id") or "chưa xác định"
    )
    nodes = ", ".join(diagnosis.get("affected_nodes") or []) or labels.get("instance", "?")
    symptom = str(annotations.get("summary") or labels.get("alertname") or "Moodle có lỗi")[:110]
    cause = str(diagnosis.get("hypothesis") or diagnosis.get("reason") or "Chưa đủ bằng chứng xác định nguyên nhân")[:150]
    lines = [f"🚨 MOODLE {scenario} · {symptom}", f"Nơi lỗi: {nodes}", f"Nguyên nhân: {cause}"]
    kind = disposition.get("disposition")
    if candidate:
        advice = _REMEDIATION_TEXT.get(str(scenario)) or str((candidate.get("action") or {}).get("reason") or "Kiểm tra hành động đã duyệt")[:150]
        lines += [f"Đề xuất: {advice}", "Tiếp theo: Safety Gate kiểm tra; Operator duyệt trước khi thực thi."]
    elif kind == "human_only_escalation":
        advice = _HUMAN_TEXT.get(str(scenario)) or str(disposition.get("reason") or "Xử lý theo runbook")[:150]
        lines += [f"Chỉ người xử lý: {advice}", "Không mở luồng approve/Executor cho sự cố này."]
    else:
        lines.append("Chưa có đề xuất an toàn; người vận hành kiểm tra telemetry và runbook.")
    steps = [str(step) for step in (admin_steps or []) if str(step).strip()]
    if not candidate and steps:
        lines.append("Admin cần làm:")
        lines += [f"{index}. {_short(step, 200)}" for index, step in enumerate(steps[:3], 1)]
    lines.append(f"Mã sự cố: {_short(report.get('incident_id', 'unassigned'), 18)}")
    return "\n".join(lines)


def format_alert_resolved(alert_name: str, instance: str) -> str:
    return (
        f"✅ Alertmanager: {alert_name} trên {instance} đã hết firing.\n"
        "Chưa phải RESOLVED: chỉ Independent Verifier mới được xác nhận sau cửa sổ ổn định."
    )


def format_safety_challenges(result: dict[str, Any]) -> str:
    """Clearly distinguish real incident input from isolated safety comparisons."""
    dangerous = result['dangerous']
    faulty = result['policy_fault']
    reviewed = result['reviewed']
    scenario = str(result.get('scenario_id') or '?')
    guidance = _REMEDIATION_TEXT.get(scenario, 'Kiểm tra bằng chứng, catalog và phương án rollback.')
    return '\n'.join([
        f"🧪 SO SÁNH SAFETY GATE · {scenario} · sự cố staging thật",
        f"Sự cố: {_short(result.get('incident_id'), 32)} · Candidate: {_short(result.get('candidate_id'), 32)}",
        f"Đề xuất kiểm thử cố định: {dangerous['action']} → {dangerous['decision']} · không thực thi.",
        f"Lý do Gate: {str(dangerous.get('reason') or 'không đạt chính sách')[:180]}",
        f"Catalog rỗng cố ý: {faulty['decision']} · Gate đối chứng riêng, không thay policy đang chạy.",
        f"Catalog chuẩn: {reviewed['decision']} · {reviewed['action']} / {reviewed['scope']}.",
        f"Admin kiểm tra: {guidance}",
        'Đề xuất nguy hiểm do bài thử tạo, không phải AI; so sánh không mở phê duyệt hay gọi Executor.',
        'Hành động thật vẫn cần Operator duyệt và Verifier xác nhận sau thực thi.',
    ])


def format_ai_shadow_proposal(result: dict[str, Any]) -> str:
    """Report actual model provenance and Gate verdict without implying dispatch."""
    proposal = result.get("proposal") or {}
    def one_line(value: Any, limit: int = 180) -> str:
        return " ".join(str(value or "?").split())[:limit]
    return "\n".join([
        f"🤖 AI SHADOW · {one_line(result.get('scenario_id'), 24)} · staging",
        f"Mô hình: {one_line(result.get('model'), 60)} · đề xuất: {one_line(proposal.get('action_id'))}",
        f"Phạm vi AI chọn: {one_line(proposal.get('target_scope'))}",
        f"Lý do AI: {one_line(proposal.get('rationale'), 240)}",
        f"Safety Gate đang vận hành: {one_line(result.get('gate_decision'), 32)} · {one_line(result.get('gate_reason'))}",
        "Chỉ đánh giá: không cấp quyền, không gọi Executor. Mức rủi ro cần người độc lập xác nhận.",
        f"Bằng chứng audit: {one_line(result.get('audit_event_id'), 40)}",
    ])


def format_prepare(candidate_id: str, prepared: dict[str, Any]) -> str:
    """Step 4: concise result of the reviewed Safety Gate checks."""
    request = prepared.get("request") or {}
    action = request.get("action") or {}
    preview = prepared.get("gate_preview") or {}
    scenario = str(request.get("scenario_id") or "?")
    lines = [f"🛡 ④ SAFETY GATE · {scenario}"]
    if preview.get("decision") == "DENY":
        lines += [
            f"Kết quả: CHẶN · {str(preview.get('reason') or 'không đạt chính sách')[:180]}",
            f"Hành động: {_short(request.get('catalog_action_id'), 90)}",
            f"Phạm vi: {_short(request.get('target_scope'), 90)} · Đích: {_short(action.get('target_resource_id'), 90)}",
            f"Sự cố: {_short(prepared.get('incident_id'), 32)} · Candidate: {_short(candidate_id, 32)}",
            *_deny_card(preview),
            "Không thực thi; không mở phê duyệt cho yêu cầu này.",
            "Admin: kiểm tra bằng chứng mới, phạm vi, catalog và rollback; không nới chính sách chỉ để vượt Gate.",
        ]
        alternative = _REMEDIATION_TEXT.get(scenario)
        if alternative:
            lines.append(f"Hướng kiểm tra theo runbook {scenario}: {alternative}")
        lines.append("Chỉ chọn phương án phù hợp bằng chứng; tạo yêu cầu mới để Gate kiểm tra lại và Operator duyệt.")
        return "\n".join(lines)
    checks = preview.get("checks") or []
    if (preview.get("decision") != "REQUIRE_APPROVAL" or not isinstance(checks, list)
            or len(checks) != len(_GATE_CHECK_LABELS)
            or any(not isinstance(item, str) or not item.strip() for item in checks)):
        lines += ["Kết quả: KHÔNG XÁC ĐỊNH · dữ liệu kiểm tra không đầy đủ.", "Không mở phê duyệt."]
        return "\n".join(lines)
    lines += [
        f"Kết quả: ĐẠT {len(checks)}/{len(_GATE_CHECK_LABELS)} kiểm tra; chờ Operator.",
        *[f"{index}. ✓ {label}" for index, label in enumerate(_GATE_CHECK_LABELS, 1)],
        f"Hành động: {_REMEDIATION_TEXT.get(scenario, str(request.get('catalog_action_id') or 'đã duyệt'))}",
        f"Đích: {action.get('target_resource_id') or request.get('target_scope')}",
        f"Request: {_short(request.get('request_id'), 8)} · chưa thực thi.",
        "Gửi `approve` trong nhóm khi bot gửi yêu cầu duyệt.",
    ]
    return "\n".join(lines)


def format_prepare_unavailable(candidate_id: str) -> str:
    """Step 4 when no fresh candidate exists; previously the operator saw nothing."""
    return "\n".join([
        "🛡 ④ SAFETY GATE · không có candidate hợp lệ",
        f"Candidate: {_short(candidate_id, 32)} đã cũ, không khớp bằng chứng mới nhất hoặc không tồn tại.",
        "Không thực thi; không mở phê duyệt.",
        "Tiếp theo: chờ alert/chẩn đoán mới rồi /prepare với candidate mới; không dùng lại request cũ.",
    ])


def format_execute(candidate_id: str, outcome: dict[str, Any]) -> str:
    """Step 5: what the gate decided and what the executor changed."""
    result = outcome.get("result") or {}
    gate = outcome.get("gate") or {}
    # The workflow status is awaiting_verification; the executor's own result says what happened.
    executed = result.get("status") == "executed" and (result.get("mutated") is True or outcome.get("mutated") is True)
    if executed:
        lines = [
            f"⚙️ ⑤ EXECUTOR · {result.get('scenario_id')} · đã thực thi",
            f"Đã sửa: {_REMEDIATION_TEXT.get(str(result.get('scenario_id')), str(result.get('catalog_action_id')))}",
            f"Node: {', '.join(result.get('completed_nodes') or []) or '?'} · Gate: {gate.get('decision')}",
            "Tiếp theo: Independent Verifier đo ổn định ≥120 giây.",
        ]
    else:
        lines = [
            "⛔ ⑤ KHÔNG THỰC THI",
            f"Gate: {gate.get('decision')} | lý do: {str(gate.get('reason') or 'none')[:200]}",
            f"trạng thái={outcome.get('status')} | mutated={outcome.get('mutated')}",
        ]
        if result.get("failure"):
            lines.append(f"failure={str(result.get('failure'))[:200]}")
        lines.append("Không có thay đổi nào ngoài những gì ghi trong audit.")
    return "\n".join(lines)


def format_execute_rejected(candidate_id: str, status_code: int, detail: str) -> str:
    return (
        "⛔ ⑤ YÊU CẦU THỰC THI BỊ TỪ CHỐI\n"
        f"HTTP {status_code}: {detail}\n"
        f"candidate={candidate_id}\n"
        "Gate không cho qua (chữ ký sai/hết hạn hoặc yêu cầu khác với candidate). Hệ thống không bị thay đổi."
    )


def format_verification(request_id: str, payload: dict[str, Any], accepted: dict[str, Any]) -> str:
    """Step 6: the signed verdict the Agent accepted."""
    samples = payload.get("probe_samples") or []
    passed = sum(1 for sample in samples if (sample.get("checks") or {}) and all(sample["checks"].values()))
    verification = payload.get("verification") or {}
    resolved = accepted.get("status") == "resolved"
    head = "🏁 ⑥ INDEPENDENT VERIFIER: RESOLVED" if resolved else f"⚠️ ⑥ INDEPENDENT VERIFIER: {accepted.get('status')}"
    rate = round(passed * 100 / len(samples)) if samples else 0
    return (
        f"{head} · {payload.get('scenario_id')}\n"
        f"Probe đạt: {passed}/{len(samples)} ({rate}%) · ổn định {verification.get('stability_seconds')} giây\n"
        f"Mã sự cố: {_short(payload.get('incident_id'), 18)} · request: {_short(request_id, 8)}\n"
        "RTO: xem tổng kết phiên chạy; RPO: không áp dụng khi không khôi phục dữ liệu."
    )


def format_verification_rejected(request_id: str, payload: dict[str, Any], status_code: int, detail: str) -> str:
    return (
        "⛔ ⑥ ATTESTATION CỦA VERIFIER BỊ TỪ CHỐI\n"
        f"incident={payload.get('incident_id')} | request={_short(request_id, 8)} | HTTP {status_code}: {detail}\n"
        "Incident KHÔNG chuyển sang RESOLVED."
    )


def notify(message: str, sender: Callable[..., Any] | None = None) -> bool:
    """Send one plain-text message; log and swallow every failure."""
    try:
        if sender is None:
            from utils.telegram_bot import send_telegram_message as sender
        return bool(sender(message, parse_mode=None))
    except Exception as exc:  # noqa: BLE001 - notification must not affect the safety path
        logger.warning("Safe-action Telegram notification failed: error_type=%s", type(exc).__name__)
        return False
