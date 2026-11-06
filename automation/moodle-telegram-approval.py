#!/usr/bin/env python3
"""Wait for one authorized Operator to reply `approve` to a specific bot prompt.

The bridge records a human decision only. The caller holds the Ed25519 private
key and signs the prepared action after approval. Reject, timeout, stale reply,
wrong chat, wrong user, or Telegram error never approves the action.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from telegram_client import TelegramClient, TelegramError  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _who(user: dict) -> str:
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return name or user.get("username") or str(user.get("id"))


def _request_text(prepared: dict) -> str:
    request = prepared.get("request") or {}
    action = request.get("action") or {}
    return "\n".join([
        "🛡 SAFETY GATE: đã kiểm tra, CHỜ OPERATOR",
        f"Kịch bản: {request.get('scenario_id')} · {request.get('catalog_action_id')}",
        f"Đích: {action.get('target_resource_id') or request.get('target_scope')}",
        f"Request: {request.get('request_id')}",
        "Reply trực tiếp tin này bằng approve để ký và thực thi; reject để dừng.",
    ])


def approve(prepared_path: Path, timeout_seconds: int, client: TelegramClient | None = None) -> int:
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    request = prepared.get("request") or {}
    if (
        prepared.get("status") != "approval_required"
        or prepared.get("execution_permitted") is not False
        or (prepared.get("gate_preview") or {}).get("decision") != "REQUIRE_APPROVAL"
        or not request.get("request_id")
        or not prepared.get("executor_action_sha256")
    ):
        print(json.dumps({"decision": "invalid", "error": "Safety Gate has not requested approval"}))
        return 2
    client = client or TelegramClient()
    if not client.configured or not client.admin_ids:
        print(json.dumps({"decision": "invalid", "error": "set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_ADMIN_USER_IDS"}))
        return 2

    try:
        offset = client.latest_update_offset(["message"])
        message_id = client.send(_request_text(prepared))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            messages, offset = client.messages(offset, timeout=min(20, max(1, int(deadline - time.monotonic()))))
            for message in messages:
                user = message.get("from") or {}
                chat = (message.get("chat") or {}).get("id")
                reply_to = (message.get("reply_to_message") or {}).get("message_id")
                if str(chat) != str(client.chat_id) or reply_to != message_id or user.get("id") not in client.admin_ids:
                    continue
                decision = str(message.get("text") or "").strip().lower()
                if decision == "reject":
                    client.edit(message_id, f"⛔ Operator {_who(user)} từ chối. Không thực thi.")
                    print(json.dumps({"decision": "rejected", "user_id": user["id"], "at": _now()}))
                    return 10
                if decision == "approve":
                    client.edit(message_id, f"✅ Operator {_who(user)} đã duyệt. Đang ký và gọi Executor.")
                    print(json.dumps({"decision": "approved", "user_id": user["id"], "at": _now(), "request_id": request["request_id"]}))
                    return 0
        client.edit(message_id, "⌛ Hết thời gian chờ Operator. Không thực thi.")
        print(json.dumps({"decision": "timeout"}))
        return 11
    except TelegramError as exc:
        print(json.dumps({"decision": "error", "error": str(exc)}))
        return 2


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
    approve_parser.add_argument("--timeout-seconds", type=int, default=600)
    who_parser = sub.add_parser("whoami")
    who_parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    return approve(args.prepared, args.timeout_seconds) if args.command == "approve" else whoami(args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
