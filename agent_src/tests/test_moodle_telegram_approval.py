"""Operator replies are bound to one prompt, one chat and an allowlisted user."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "automation" / "lib"))
spec = importlib.util.spec_from_file_location("telegram_approval", REPO_ROOT / "automation" / "moodle-telegram-approval.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

ADMIN, STRANGER, CHAT = 111, 999, "-100123"
PREPARED = {
    "status": "approval_required", "execution_permitted": False,
    "gate_preview": {"decision": "REQUIRE_APPROVAL"},
    "executor_action_sha256": "a" * 64,
    "request": {"request_id": "req-1", "scenario_id": "CON-01", "catalog_action_id": "start_reviewed_compose_service",
                "action": {"target_resource_id": "moodle-app-b"}},
}


class FakeClient:
    def __init__(self, replies):
        self.replies, self.admin_ids, self.chat_id, self.configured = list(replies), {ADMIN}, CHAT, True
        self.texts = []

    def latest_update_offset(self, allowed_updates=None):
        assert allowed_updates == ["message"]
        return 7

    def send(self, text, buttons=None):
        self.texts.append(text)
        return 42

    def edit(self, message_id, text, buttons=None):
        assert message_id == 42
        self.texts.append(text)

    def messages(self, offset, timeout=20):
        if not self.replies:
            return [], offset
        user, chat, reply_to, text = self.replies.pop(0)
        return [{"from": {"id": user, "first_name": "Tester"}, "chat": {"id": chat},
                 "reply_to_message": {"message_id": reply_to}, "text": text}], offset + 1


def _run(tmp_path, replies, capsys, timeout=1):
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps(PREPARED), encoding="utf-8")
    client = FakeClient(replies)
    code = bridge.approve(prepared, timeout, client=client)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1]), client


def test_one_operator_reply_approves(tmp_path, capsys) -> None:
    code, out, client = _run(tmp_path, [(ADMIN, int(CHAT), 42, "approve")], capsys)
    assert code == 0 and out["decision"] == "approved" and out["request_id"] == "req-1"
    assert "Đang ký" in client.texts[-1]


def test_reject_stops(tmp_path, capsys) -> None:
    code, out, client = _run(tmp_path, [(ADMIN, int(CHAT), 42, "reject")], capsys)
    assert code == 10 and out["decision"] == "rejected"
    assert "Không thực thi" in client.texts[-1]


def test_wrong_user_chat_and_reply_are_ignored(tmp_path, capsys) -> None:
    replies = [(STRANGER, int(CHAT), 42, "approve"), (ADMIN, 123, 42, "approve"),
               (ADMIN, int(CHAT), 41, "approve"), (ADMIN, int(CHAT), 42, "approved")]
    code, out, client = _run(tmp_path, replies, capsys)
    assert code == 11 and out["decision"] == "timeout"
    assert "Hết thời gian" in client.texts[-1]


def test_missing_admin_list_fails_closed(tmp_path, capsys) -> None:
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps(PREPARED), encoding="utf-8")
    client = FakeClient([])
    client.admin_ids = set()
    assert bridge.approve(prepared, 5, client=client) == 2


def test_gate_denial_cannot_be_approved(tmp_path, capsys) -> None:
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps({**PREPARED, "gate_preview": {"decision": "DENY"}}), encoding="utf-8")
    assert bridge.approve(prepared, 5, client=FakeClient([])) == 2
