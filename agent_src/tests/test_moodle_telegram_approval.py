"""Operator-side Telegram approval bridge: humans decide, nothing approves by default."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "automation" / "lib"))
_spec = importlib.util.spec_from_file_location("telegram_approval", REPO_ROOT / "automation" / "moodle-telegram-approval.py")
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

ADMIN, STRANGER, CHAT = 111, 999, "-100123"
PREPARED = {
    "status": "approval_required",
    "execution_permitted": False,
    "gate_action_sha256": "a" * 64,
    "request": {"scenario_id": "CON-01", "catalog_action_id": "start_reviewed_compose_service",
                "action": {"target_resource_id": "moodle-app-b", "evidence_refs": ["e"] * 8}},
}


class FakeClient:
    """Replays scripted button presses; records what the bridge shows."""

    def __init__(self, presses):
        self.presses, self.admin_ids, self.chat_id, self.configured = list(presses), {ADMIN}, CHAT, True
        self.texts, self.toasts, self.nonce = [], [], None

    def latest_update_offset(self):
        return 0

    def send(self, text, buttons=None):
        self.texts.append(text)
        self.nonce = buttons[0][0]["callback_data"].split(":")[1]
        return 1

    def edit(self, message_id, text, buttons=None):
        self.texts.append(text)

    def answer(self, callback_id, text):
        self.toasts.append(text)

    def callback_queries(self, offset, timeout=20):
        if not self.presses:
            return [], offset
        user, role, verb, nonce = self.presses.pop(0)
        data = f"apv:{nonce or self.nonce}:{role}:{verb}"
        return [{"id": "q", "data": data, "from": {"id": user, "first_name": "Tester"},
                 "message": {"chat": {"id": int(CHAT)}}}], offset + 1


def _run(tmp_path, presses, capsys, timeout=60):
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps(PREPARED), encoding="utf-8")
    client = FakeClient(presses)
    code = bridge.approve(prepared, timeout, client=client)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1]), client


def test_both_roles_approve_in_order(tmp_path, capsys) -> None:
    code, out, client = _run(tmp_path, [(ADMIN, "safety-officer", "ok", None), (ADMIN, "operator", "ok", None)], capsys)
    assert code == 0 and out["decision"] == "approved" and out["same_human"] is True
    assert set(out["approvals"]) == {"safety-officer", "operator"}
    assert "Đủ 2 phê duyệt" in client.texts[-1]


def test_reject_stops_without_approval(tmp_path, capsys) -> None:
    code, out, client = _run(tmp_path, [(ADMIN, "safety-officer", "no", None)], capsys)
    assert code == 10 and out["decision"] == "rejected"
    assert "BỊ TỪ CHỐI" in client.texts[-1]


def test_non_admin_and_out_of_order_presses_are_ignored(tmp_path, capsys) -> None:
    presses = [
        (STRANGER, "safety-officer", "ok", None),      # not an admin
        (ADMIN, "operator", "ok", None),               # wrong order
        (ADMIN, "safety-officer", "ok", "deadbeef"),   # button from another request
    ]
    code, out, client = _run(tmp_path, presses, capsys, timeout=1)
    assert code == 11 and out["decision"] == "timeout" and out["approvals"] == {}
    assert any("không có quyền" in toast for toast in client.toasts)


def test_missing_admin_list_fails_closed(tmp_path, capsys) -> None:
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps(PREPARED), encoding="utf-8")
    client = FakeClient([])
    client.admin_ids = set()
    assert bridge.approve(prepared, 5, client=client) == 2
