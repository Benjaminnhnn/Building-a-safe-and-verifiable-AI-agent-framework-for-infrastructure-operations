#!/usr/bin/env python3
"""Operator-side Telegram approval bridge for one prepared Moodle safe action.

The Agent prepares a request and the Safety Gate explains its checks; this
bridge asks a human on Telegram to approve or reject it, one role at a time
(safety-officer, then operator). It never signs and never calls the Agent:
it only prints the human decision. The drill signs with the operator's local
Ed25519 keys only after this script exits 0, so the Agent never holds a
signing key and cannot approve its own action.

Only presses from TELEGRAM_ADMIN_USER_IDS in TELEGRAM_CHAT_ID count. Buttons
carry a random nonce, so presses on an older request are ignored. Rejection,
timeout or any Telegram error exits non-zero: nothing is approved by default.

    python3 automation/moodle-telegram-approval.py approve --prepared <prepared.json>
    python3 automation/moodle-telegram-approval.py whoami    # find your user ID

Exit codes: 0 approved, 10 rejected, 11 timed out, 2 configuration or Telegram error.
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

ROLES = ("safety-officer", "operator")
ROLE_LABEL = {"safety-officer": "Safety officer", "operator": "Operator"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _who(user: dict) -> str:
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return name or user.get("username") or str(user.get("id"))


def _request_text(prepared: dict, signed: dict[str, dict], state: str) -> str:
    request = prepared.get("request") or {}
    action = request.get("action") or {}
    rollback = (action.get("rollback_plan") or {}).get("rollback_action_id")
    lines = [
        "✍️ YÊU CẦU PHÊ DUYỆT (Safety Gate: REQUIRE_APPROVAL)",
        f"{request.get('scenario_id')}: {request.get('catalog_action_id')} -> {action.get('target_resource_id')}",
        f"Lý do: {str(action.get('reason') or '')[:160]}",
        f"Rollback: {rollback or 'không có'} | bằng chứng: {len(action.get('evidence_refs') or [])}",
        f"Gate hash: {str(prepared.get('gate_action_sha256'))[:16]}…",
        "",
    ]
    for role in ROLES:
        if role in signed:
            lines.append(f"✅ {ROLE_LABEL[role]}: {signed[role]['name']} duyệt lúc {signed[role]['at']}")
        else:
            lines.append(f"⏳ {ROLE_LABEL[role]}: chờ duyệt")
    lines += ["", state]
    return "\n".join(lines)


def _buttons(nonce: str, role: str) -> list[list[dict[str, str]]]:
    return [[
        {"text": f"✅ Duyệt ({ROLE_LABEL[role]})", "callback_data": f"apv:{nonce}:{role}:ok"},
        {"text": "❌ Từ chối", "callback_data": f"apv:{nonce}:{role}:no"},
    ]]


def approve(prepared_path: Path, timeout_seconds: int, client: TelegramClient | None = None) -> int:
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    if prepared.get("status") != "approval_required" or prepared.get("execution_permitted") is not False:
        print(json.dumps({"decision": "invalid", "error": "prepared request is not awaiting approval"}))
        return 2
    client = client or TelegramClient()
    if not client.configured or not client.admin_ids:
        print(json.dumps({"decision": "invalid", "error": "set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_ADMIN_USER_IDS"}))
        return 2

    nonce = secrets.token_hex(4)
    signed: dict[str, dict] = {}
    try:
        offset = client.latest_update_offset()
        message_id = client.send(
            _request_text(prepared, signed, "Bấm Duyệt cho vai Safety officer trước."), _buttons(nonce, ROLES[0])
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            queries, offset = client.callback_queries(offset, timeout=min(20, max(1, int(deadline - time.monotonic()))))
            for query in queries:
                parts = str(query.get("data", "")).split(":")
                if len(parts) != 4 or parts[0] != "apv" or parts[1] != nonce:
                    continue
                user = query.get("from") or {}
                chat = ((query.get("message") or {}).get("chat") or {}).get("id")
                if user.get("id") not in client.admin_ids or str(chat) != str(client.chat_id):
                    client.answer(query["id"], "Bạn không có quyền duyệt yêu cầu này.")
                    continue
                role, verb = parts[2], parts[3]
                expected = next(r for r in ROLES if r not in signed)
                if role != expected:
                    client.answer(query["id"], f"Đang chờ vai {ROLE_LABEL[expected]}.")
                    continue
                if verb == "no":
                    client.answer(query["id"], "Đã từ chối. Không thực thi.")
                    client.edit(message_id, _request_text(
                        prepared, signed, f"⛔ BỊ TỪ CHỐI bởi {_who(user)} ({ROLE_LABEL[role]}) lúc {_now()}. Không thực thi."))
                    print(json.dumps({"decision": "rejected", "role": role, "user_id": user.get("id"), "at": _now()}))
                    return 10
                signed[role] = {"user_id": user.get("id"), "name": _who(user), "at": _now()}
                client.answer(query["id"], f"Đã ghi nhận duyệt vai {ROLE_LABEL[role]}.")
                if len(signed) == len(ROLES):
                    same = signed["safety-officer"]["user_id"] == signed["operator"]["user_id"]
                    client.edit(message_id, _request_text(
                        prepared, signed,
                        "🔏 Đủ 2 phê duyệt. Laptop operator đang ký Ed25519 (khoá không nằm trên Agent) và gửi yêu cầu thực thi."
                        + ("\nGhi chú: cùng một người duyệt hai vai (giới hạn demo)." if same else "")))
                    print(json.dumps({"decision": "approved", "approvals": signed, "same_human": same}))
                    return 0
                client.edit(message_id, _request_text(
                    prepared, signed, f"Bấm Duyệt cho vai {ROLE_LABEL[ROLES[1]]}."), _buttons(nonce, ROLES[1]))
        client.edit(message_id, _request_text(prepared, signed, f"⌛ HẾT THỜI GIAN chờ duyệt lúc {_now()}. Không thực thi."))
        print(json.dumps({"decision": "timeout", "approvals": signed}))
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("--prepared", type=Path, required=True)
    approve_parser.add_argument("--timeout-seconds", type=int, default=600)
    who_parser = sub.add_parser("whoami")
    who_parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    if args.command == "approve":
        return approve(args.prepared, args.timeout_seconds)
    return whoami(args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
