#!/usr/bin/env python3
"""Wait for one authorized Operator to send `approve` after the bot prompt.

The bridge records a human decision only. The caller holds the Ed25519 private
key and signs the prepared action after approval. Wrong chat, wrong user,
stale messages, replies to a different prompt, or concurrent local requests
never approve the action.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from telegram_client import TelegramClient, TelegramError  # noqa: E402

GATE_CHECKS = (
    "Cấu trúc yêu cầu hợp lệ",
    "Kill switch đang tắt",
    "Có ít nhất 3 bằng chứng khác nhau",
    "Hành động và thời gian thực thi nằm trong catalog",
    "Có phương án rollback đối ứng",
    "Đúng vai trò, phạm vi và môi trường",
    "Mục tiêu và loại tài nguyên được phép",
    "Độ tin cậy chẩn đoán đạt ngưỡng",
)
REMEDIATION_TEXT = {
    "BIZ-01": "Tắt chế độ bảo trì sau khi xác nhận Moodle không nâng cấp.",
    "BIZ-02": "Khởi động lại Moodle cron trên node A; giữ dữ liệu hàng đợi.",
    "CON-01": "Khởi động lại Moodle web trên node B bằng Compose đã duyệt.",
    "DB-01": "Gỡ đúng rule chặn TCP 5432 tới RDS trên hai node Moodle.",
    "RES-01": "Dừng container tải CPU được gắn nhãn lỗi trên node B.",
    "NET-01": "Tạo lại web container để bỏ bản ghi /etc/hosts sai.",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _who(user: dict) -> str:
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return name or user.get("username") or str(user.get("id"))


def _request_text(prepared: dict, timeout_seconds: int = 0) -> str:
    request = prepared.get("request") or {}
    action = request.get("action") or {}
    scenario = request.get("scenario_id")
    checks = (prepared.get("gate_preview") or {}).get("checks") or []
    lab = prepared.get("isolated_lab") is True
    return "\n".join([
        "🧪 LAB CÔ LẬP · SAFETY GATE: ĐẠT 8/8, CHỜ OPERATOR" if lab else "🛡 SAFETY GATE: ĐẠT 8/8, CHỜ OPERATOR",
        f"Kịch bản: {scenario}",
        "Hành động: sửa dịch vụ lab và cố ý dừng một phụ thuộc lab để kiểm thử; không sửa Moodle/AWS." if lab else f"Hành động: {REMEDIATION_TEXT.get(scenario, request.get('catalog_action_id'))}",
        "Phạm vi: lab cô lập; catalog dùng để đánh giá chính sách." if lab else f"Phạm vi: {request.get('target_scope') or 'chưa xác định'} · Tài nguyên: {action.get('target_resource_id') or 'chưa xác định'}",
        *[f"{index}. ✓ {label}" for index, label in enumerate(GATE_CHECKS, 1) if index <= len(checks)],
        f"Request: {request.get('request_id')}",
        "Gửi approve trong nhóm để duyệt; gửi reject để dừng. Bạn cũng có thể Reply tin này.",
        "Thời gian chờ: không giới hạn." if timeout_seconds == 0 else f"Thời gian chờ: {timeout_seconds} giây.",
    ])


def _closed_request_text(prepared: dict, timeout_seconds: int, decision: str) -> str:
    lines = _request_text(prepared, timeout_seconds).splitlines()
    lines[0] = lines[0].replace("CHỜ OPERATOR", decision)
    return "\n".join(lines[:-2])


def approve(prepared_path: Path, timeout_seconds: int, client: TelegramClient | None = None) -> int:
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    request = prepared.get("request") or {}
    checks = (prepared.get("gate_preview") or {}).get("checks")
    if (
        timeout_seconds < 0
        or prepared.get("status") != "approval_required"
        or prepared.get("execution_permitted") is not False
        or (prepared.get("gate_preview") or {}).get("decision") != "REQUIRE_APPROVAL"
        or not isinstance(checks, list)
        or len(checks) != len(GATE_CHECKS)
        or any(not isinstance(item, str) or not item.strip() for item in checks)
        or not request.get("request_id")
        or not prepared.get("executor_action_sha256")
    ):
        print(json.dumps({"decision": "invalid", "error": "Safety Gate has not requested approval"}))
        return 2
    client = client or TelegramClient()
    if not client.configured or not client.admin_ids:
        print(json.dumps({"decision": "invalid", "error": "set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_ADMIN_USER_IDS"}))
        return 2

    lock_dir = Path(os.getenv("MOODLE_APPROVAL_LOCK_DIR", "~/.config/moodle-aiops")).expanduser()
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_dir / ("telegram-approval-" + hashlib.sha256(str(client.chat_id).encode()).hexdigest()[:16] + ".lock")
    lock_file = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        print(json.dumps({"decision": "invalid", "error": "another approval is already pending for this chat"}))
        return 2
    try:
        offset = client.latest_update_offset(["message"])
        message_id = client.send(_request_text(prepared, timeout_seconds))
        deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
        while deadline is None or time.monotonic() < deadline:
            poll_timeout = 20 if deadline is None else min(20, max(1, int(deadline - time.monotonic())))
            try:
                messages, offset = client.messages(offset, timeout=poll_timeout)
            except TelegramError as exc:
                if deadline is not None:
                    raise
                print(f"Telegram polling interrupted ({exc}); retrying in 5 seconds.", file=sys.stderr)
                time.sleep(5)
                continue
            for message in messages:
                user = message.get("from") or {}
                chat = (message.get("chat") or {}).get("id")
                reply = message.get("reply_to_message")
                reply_to = (reply or {}).get("message_id")
                incoming_id = message.get("message_id")
                if (str(chat) != str(client.chat_id) or user.get("id") not in client.admin_ids
                        or type(incoming_id) is not int or incoming_id <= message_id
                        or (reply is not None and reply_to != message_id)):
                    continue
                decision = str(message.get("text") or "").strip().lower()
                if decision == "reject":
                    text = _closed_request_text(prepared, timeout_seconds, "ĐÃ TỪ CHỐI")
                    client.edit(message_id, text + f"\n⛔ Operator {_who(user)} từ chối. Không thực thi.")
                    print(json.dumps({"decision": "rejected", "user_id": user["id"], "at": _now()}))
                    return 10
                if decision == "approve":
                    text = _closed_request_text(prepared, timeout_seconds, "ĐÃ DUYỆT")
                    client.edit(message_id, text + f"\n✅ Operator {_who(user)} đã duyệt. Đang ký và kiểm tra lại trước thực thi.")
                    print(json.dumps({"decision": "approved", "user_id": user["id"], "at": _now(), "request_id": request["request_id"]}))
                    return 0
        client.edit(message_id, "⌛ Hết thời gian chờ Operator. Không thực thi.")
        print(json.dumps({"decision": "timeout"}))
        return 11
    except TelegramError as exc:
        print(json.dumps({"decision": "error", "error": str(exc)}))
        return 2
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def whoami(timeout_seconds: int) -> int:
    client = TelegramClient()
    try:
        offset = client.latest_update_offset()
        nonce = secrets.token_hex(4)
        message_id = client.send(
            "Bấm nút để lấy Telegram user ID của bạn (dùng cho TELEGRAM_ADMIN_USER_IDS).",
            [[{"text": "Tôi là người duyệt", "callback_data": f"who:{nonce}"}]],
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            queries, offset = client.callback_queries(offset)
            for query in queries:
                if query.get("data") == f"who:{nonce}":
                    user = query.get("from") or {}
                    client.answer(query["id"], f"User ID: {user.get('id')}")
                    client.edit(message_id, f"Đã lấy user ID của {_who(user)}.")
                    print(f"{_who(user)}: TELEGRAM_ADMIN_USER_IDS={user.get('id')}")
                    return 0
        print("Không có ai bấm nút trong thời gian chờ.", file=sys.stderr)
        return 11
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("--prepared", type=Path, required=True)
    approve_parser.add_argument("--timeout-seconds", type=int, default=0, help="0 means wait until an Operator responds")
    who_parser = sub.add_parser("whoami")
    who_parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    return approve(args.prepared, args.timeout_seconds) if args.command == "approve" else whoami(args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
