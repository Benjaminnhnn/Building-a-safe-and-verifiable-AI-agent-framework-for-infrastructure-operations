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
    "Schema yêu cầu hợp lệ",
    "Kill switch đang tắt",
    "Đủ bằng chứng",
    "Hành động có trong catalog đã review",
    "Có rollback đối ứng",
    "Đúng RBAC, phạm vi và môi trường staging",
    "Mục tiêu nằm trong phạm vi được phép",
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
    "SSO-04": "Kiểm tra assignment hoặc group của người dùng trên Okta.",
    "SSO-13": "Kiểm tra OAuth 2 issuer Okta và nút đăng nhập trong Moodle.",
}


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


def format_alert_report(alert: dict[str, Any], report: dict[str, Any]) -> str:
    """Short operator-facing alert; the detailed audit remains in evidence."""
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
    lines.append(f"Mã sự cố: {_short(report.get('incident_id', 'unassigned'), 18)}")
    return "\n".join(lines)


def format_alert_resolved(alert_name: str, instance: str) -> str:
    return (
        f"✅ Alertmanager: {alert_name} trên {instance} đã hết firing.\n"
        "Chưa phải RESOLVED: chỉ Independent Verifier mới được xác nhận sau cửa sổ ổn định."
    )


def format_prepare(candidate_id: str, prepared: dict[str, Any]) -> str:
    """Step 4: concise result of the reviewed Safety Gate checks."""
    request = prepared.get("request") or {}
    action = request.get("action") or {}
    preview = prepared.get("gate_preview") or {}
    scenario = str(request.get("scenario_id") or "?")
    lines = [f"🛡 ④ SAFETY GATE · {scenario}"]
    if preview.get("decision") == "DENY":
        lines += [f"Kết quả: CHẶN · {str(preview.get('reason') or 'không đạt chính sách')[:180]}", "Không mở phê duyệt."]
        return "\n".join(lines)
    checks = preview.get("checks") or []
    lines += [
        f"Kết quả: ĐẠT {len(checks)}/{len(_GATE_CHECK_LABELS)} kiểm tra; chờ Operator.",
        f"Hành động: {_REMEDIATION_TEXT.get(scenario, str(request.get('catalog_action_id') or 'đã duyệt'))}",
        f"Đích: {action.get('target_resource_id') or request.get('target_scope')}",
        f"Request: {_short(request.get('request_id'), 8)} · chưa thực thi.",
        "Reply `approve` vào tin yêu cầu của bot để xác nhận hành động này.",
    ]
    return "\n".join(lines)


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
