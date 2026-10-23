"""Telegram summaries for the Moodle safe-action steps after diagnosis.

Steps 4 (Safety Gate prepare), 5 (executor dispatch) and 6 (Independent
Verifier acceptance) happen in the Agent API, not in the Celery alert task, so
they are reported here. Messages carry identifiers, decisions and short hash
prefixes only: never approval signatures, tokens or request bodies.

Notification is best effort and runs after the API response is sent. A slow
or failing Telegram call must never change a safety decision.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _short(value: Any, length: int = 12) -> str:
    text = str(value or "")
    return f"{text[:length]}…" if len(text) > length else (text or "none")


def format_prepare(candidate_id: str, prepared: dict[str, Any]) -> str:
    request = prepared.get("request") or {}
    action = request.get("action") or {}
    return (
        "④ Safety Gate: cần phê duyệt\n"
        f"incident={prepared.get('incident_id')} | scenario={request.get('scenario_id')}\n"
        f"action={request.get('catalog_action_id')} -> {action.get('target_resource_id')} "
        f"scope={request.get('target_scope')}\n"
        f"candidate={candidate_id} | confidence={prepared.get('diagnosis_confidence')}\n"
        f"gate_hash={_short(prepared.get('gate_action_sha256'))}\n"
        "Cần 2 chữ ký độc lập (safety-officer + operator) trước khi thực thi. "
        f"execution_permitted={prepared.get('execution_permitted', False)}"
    )


def format_execute(candidate_id: str, outcome: dict[str, Any]) -> str:
    result = outcome.get("result") or {}
    gate = outcome.get("gate") or {}
    executed = outcome.get("status") == "executed" and outcome.get("mutated") is True
    head = "⑤ Executor: đã thực thi" if executed else f"⑤ Executor: KHÔNG thực thi ({outcome.get('status')})"
    lines = [
        head,
        f"scenario={result.get('scenario_id')} | action={result.get('catalog_action_id')} "
        f"scope={result.get('target_scope')}",
        f"request={result.get('request_id')} | candidate={candidate_id}",
        f"gate={gate.get('decision')} | mutated={outcome.get('mutated')} | nodes={result.get('completed_nodes')}",
    ]
    if result.get("failure"):
        lines.append(f"failure={str(result.get('failure'))[:200]}")
    if executed:
        lines.append("Incident chờ Independent Verifier (cửa sổ ổn định ≥120 s). Chưa RESOLVED.")
    else:
        lines.append(f"reason={str(gate.get('reason') or 'none')[:200]}")
    return "\n".join(lines)


def format_execute_rejected(candidate_id: str, status_code: int, detail: str) -> str:
    return (
        "⑤ Executor: yêu cầu bị từ chối trước khi thực thi\n"
        f"candidate={candidate_id} | HTTP {status_code}: {detail}\n"
        "Không có thay đổi nào trên hệ thống."
    )


def format_verification(request_id: str, payload: dict[str, Any], accepted: dict[str, Any]) -> str:
    samples = payload.get("probe_samples") or []
    passed = sum(1 for sample in samples if all((sample.get("checks") or {}).values()))
    verification = payload.get("verification") or {}
    resolved = accepted.get("status") == "resolved"
    head = "⑥ Independent Verifier: RESOLVED" if resolved else f"⑥ Independent Verifier: {accepted.get('status')}"
    return (
        f"{head}\n"
        f"incident={payload.get('incident_id')} | scenario={payload.get('scenario_id')} | request={request_id}\n"
        f"mẫu probe đạt {passed}/{len(samples)} | ổn định {verification.get('stability_seconds')} s | "
        f"verdict={verification.get('verdict')}\n"
        f"event_hash={_short(accepted.get('event_hash'))}"
        + (" (đã ghi nhận trước đó)" if accepted.get("idempotent") else "")
    )


def format_verification_rejected(request_id: str, payload: dict[str, Any], status_code: int, detail: str) -> str:
    return (
        "⑥ Independent Verifier: attestation bị từ chối\n"
        f"incident={payload.get('incident_id')} | request={request_id} | HTTP {status_code}: {detail}\n"
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
