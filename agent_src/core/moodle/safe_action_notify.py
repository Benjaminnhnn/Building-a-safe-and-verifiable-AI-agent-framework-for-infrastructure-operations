"""Telegram summaries for the Moodle incident workflow.

Step 2/3 (alert and diagnosis) is sent by the Celery worker; steps 4 (Safety
Gate prepare), 5 (executor dispatch) and 6 (Independent Verifier acceptance)
by the Agent API. Approval buttons are sent by the operator-side bridge
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
    """Steps 2-3: what fired, what the Agent concluded, and what happens next."""
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    diagnosis = _as_dict(report.get("diagnosis"))
    disposition = _as_dict(report.get("remediation_disposition"))
    candidate = _as_dict(report.get("remediation_candidate"))
    action = candidate.get("action") or {}
    scenario = (
        disposition.get("scenario_id") or candidate.get("scenario_id") or report.get("scenario_id") or "chưa xác định"
    )
    nodes = ", ".join(diagnosis.get("affected_nodes") or []) or labels.get("instance", "?")
    evidence_count = len(action.get("evidence_refs") or diagnosis.get("evidence_refs") or [])

    lines = [
        f"🚨 ② ALERT {labels.get('alertname', 'Unknown')} [{scenario}]",
        f"Ở đâu: {nodes}",
    ]
    if annotations.get("summary"):
        lines.append(f"Hiện tượng: {str(annotations['summary'])[:200]}")
    lines.append("")
    lines.append("🧠 ③ AI CHẨN ĐOÁN")
    if diagnosis.get("hypothesis"):
        lines.append(f"Nguyên nhân: {str(diagnosis['hypothesis'])[:220]}")
        lines.append(
            f"Mã: {diagnosis.get('cause_code')} | độ tin cậy {diagnosis.get('confidence')} | {evidence_count} bằng chứng"
        )
    else:
        lines.append(f"Không kết luận được: {str(diagnosis.get('reason') or report.get('status'))[:200]}")

    kind = disposition.get("disposition")
    lines.append("")
    if candidate:
        rollback = (action.get("rollback_plan") or {}).get("rollback_action_id")
        lines += [
            "🛠 ĐỀ XUẤT KHẮC PHỤC",
            f"{candidate.get('catalog_action_id')} -> {action.get('target_resource_id')} (phạm vi {candidate.get('target_scope')})",
            f"Rollback: {rollback or 'không có'}",
            "",
            "➡️ Tiếp theo: Safety Gate kiểm tra và cần 2 chữ ký người duyệt.",
            "AI KHÔNG tự thực thi (execution_permitted=False).",
        ]
    elif kind == "human_only_escalation":
        lines += [
            "🙅 CHỈ NGƯỜI ĐƯỢC SỬA",
            f"Hành động cần làm: {disposition.get('catalog_action_id')} (phạm vi {disposition.get('target_scope')})",
            "Agent không có quyền thực thi; executor cũng từ chối lệnh này.",
            "➡️ Operator sửa tay theo runbook, sau đó probe xác nhận ổn định ≥120 s.",
        ]
    else:
        lines += [
            "➡️ Không có đề xuất tự động: chuyển người xử lý.",
            f"Lý do: {str(disposition.get('reason') or report.get('status'))[:200]}",
        ]
    lines.append(f"incident={report.get('incident_id', 'unassigned')}")
    return "\n".join(lines)


def format_alert_resolved(alert_name: str, instance: str) -> str:
    return (
        f"✅ Alertmanager: {alert_name} trên {instance} đã hết firing.\n"
        "Chưa phải RESOLVED: chỉ Independent Verifier mới được xác nhận sau cửa sổ ổn định."
    )


def format_prepare(candidate_id: str, prepared: dict[str, Any]) -> str:
    """Step 4: each Safety Gate check, and why a human must still approve."""
    request = prepared.get("request") or {}
    action = request.get("action") or {}
    preview = prepared.get("gate_preview") or {}
    lines = [
        "🛡 ④ SAFETY GATE",
        f"{request.get('scenario_id')}: {request.get('catalog_action_id')} -> {action.get('target_resource_id')}",
        f"incident={prepared.get('incident_id')} | request={_short(request.get('request_id'), 8)}",
        "",
    ]
    if preview.get("decision") == "DENY":
        lines += [f"⛔ BỊ CHẶN: {preview.get('reason')}", "Không có yêu cầu phê duyệt nào được tạo."]
        return "\n".join(lines)
    for label, detail in zip(_GATE_CHECK_LABELS, preview.get("checks") or []):
        lines.append(f"✅ {label}: {detail}")
    reasons = preview.get("approval_reasons") or []
    lines += [
        "",
        "⏸ CHƯA ĐƯỢC THỰC THI: cần 2 chữ ký độc lập (safety-officer + operator)",
    ]
    if reasons:
        lines.append("Vì: " + "; ".join(reasons))
    lines += [
        f"Chữ ký phải gắn đúng hash {_short(prepared.get('gate_action_sha256'))}, hết hạn sau 5 phút.",
        f"execution_permitted={prepared.get('execution_permitted', False)}",
    ]
    return "\n".join(lines)


def format_execute(candidate_id: str, outcome: dict[str, Any]) -> str:
    """Step 5: what the gate decided with both signatures, and what changed."""
    result = outcome.get("result") or {}
    gate = outcome.get("gate") or {}
    # The workflow status is awaiting_verification; the executor's own result says what happened.
    executed = result.get("status") == "executed" and (result.get("mutated") is True or outcome.get("mutated") is True)
    if executed:
        lines = [
            "⚙️ ⑤ EXECUTOR ĐÃ THỰC THI",
            f"Gate: {gate.get('decision')} (đủ 2 chữ ký hợp lệ, đúng hash, còn hạn)",
            f"{result.get('scenario_id')}: {result.get('catalog_action_id')} trên node {result.get('completed_nodes')}",
            f"mutated=True | request={_short(result.get('request_id'), 8)}",
            "",
            "➡️ Incident chuyển VERIFYING. Chưa RESOLVED: chờ Independent Verifier (cửa sổ ổn định ≥120 s).",
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
    passed = sum(1 for sample in samples if all((sample.get("checks") or {}).values()))
    verification = payload.get("verification") or {}
    resolved = accepted.get("status") == "resolved"
    head = "🏁 ⑥ INDEPENDENT VERIFIER: RESOLVED" if resolved else f"⚠️ ⑥ INDEPENDENT VERIFIER: {accepted.get('status')}"
    return (
        f"{head}\n"
        f"{payload.get('scenario_id')} | incident={payload.get('incident_id')} | request={_short(request_id, 8)}\n"
        f"{passed}/{len(samples)} mẫu đạt mọi probe | ổn định {verification.get('stability_seconds')} s\n"
        f"Attestation Ed25519 hợp lệ, gắn với biên nhận thực thi; event={_short(accepted.get('event_hash'))}"
        + (" (đã ghi nhận trước đó)" if accepted.get("idempotent") else "")
        + "\nChỉ Verifier được chuyển incident sang RESOLVED; Agent và Executor không tự làm được."
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
