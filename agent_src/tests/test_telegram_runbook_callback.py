import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import main
from core import runbook_registry


client = TestClient(main.app)
WEBHOOK_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"}


def _tool_metadata() -> dict:
    return {
        "name": "check_redis_queue_depth",
        "version": "1.0.0",
        "description": "Read Celery queue depth from Redis broker",
        "risk_level": "read_only",
        "related_services": ["redis", "celery"],
        "runbook_tags": ["redis-broker"],
    }


def _callback_payload(action: str, draft_id: str, chat_id: str = "123") -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "callback-1",
            "data": f"runbook:{action}:{draft_id}",
            "from": {"id": 456, "username": "admin-user"},
            "message": {"chat": {"id": chat_id}},
        },
    }


def test_telegram_callback_approves_and_publishes_runbook_draft() -> None:
    with TemporaryDirectory() as tmp:
        workflow_dir = os.path.join(tmp, "workflow")
        kb_dir = os.path.join(tmp, "knowledge_base")
        os.makedirs(kb_dir)
        with patch.dict(os.environ, {"RUNBOOK_WORKFLOW_DIR": workflow_dir, "KNOWLEDGE_BASE_DIR": kb_dir}):
            revision = runbook_registry.save_tool_revision(_tool_metadata(), actor="admin")
            draft = runbook_registry.create_runbook_draft(revision["name"], revision["revision_id"])

            with (
                patch.object(main.telegram_bot, "TELEGRAM_CHAT_ID", "123"),
                patch.object(main.telegram_bot, "TELEGRAM_TOKEN", "token"),
                patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "telegram-secret"),
                patch.object(main, "TELEGRAM_ADMIN_USER_IDS", {"456"}),
                patch.object(main, "_answer_telegram_callback"),
                patch.object(main, "get_rag_instance", return_value=None),
                patch.object(main, "send_telegram_message"),
            ):
                response = client.post("/telegram/webhook", json=_callback_payload("approve", draft["draft_id"]), headers=WEBHOOK_HEADERS)

        assert response.status_code == 200
        assert response.json()["status"] == "published"
        assert os.path.exists(response.json()["draft"]["published_path"])


def test_telegram_callback_rejects_runbook_draft() -> None:
    with TemporaryDirectory() as tmp:
        workflow_dir = os.path.join(tmp, "workflow")
        kb_dir = os.path.join(tmp, "knowledge_base")
        os.makedirs(kb_dir)
        with patch.dict(os.environ, {"RUNBOOK_WORKFLOW_DIR": workflow_dir, "KNOWLEDGE_BASE_DIR": kb_dir}):
            revision = runbook_registry.save_tool_revision(_tool_metadata(), actor="admin")
            draft = runbook_registry.create_runbook_draft(revision["name"], revision["revision_id"])

            with (
                patch.object(main.telegram_bot, "TELEGRAM_CHAT_ID", "123"),
                patch.object(main.telegram_bot, "TELEGRAM_TOKEN", "token"),
                patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "telegram-secret"),
                patch.object(main, "TELEGRAM_ADMIN_USER_IDS", {"456"}),
                patch.object(main, "_answer_telegram_callback"),
                patch.object(main, "send_telegram_message"),
            ):
                response = client.post("/telegram/webhook", json=_callback_payload("reject", draft["draft_id"]), headers=WEBHOOK_HEADERS)

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        assert response.json()["draft"]["status"] == "rejected"


def test_telegram_callback_rejects_unknown_chat() -> None:
    with (
        patch.object(main.telegram_bot, "TELEGRAM_CHAT_ID", "123"),
        patch.object(main.telegram_bot, "TELEGRAM_TOKEN", "token"),
        patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "telegram-secret"),
        patch.object(main, "TELEGRAM_ADMIN_USER_IDS", {"456"}),
        patch.object(main, "_answer_telegram_callback"),
    ):
        response = client.post("/telegram/webhook", json=_callback_payload("approve", "draft-1", chat_id="456"), headers=WEBHOOK_HEADERS)

    assert response.status_code == 403


def test_telegram_webhook_rejects_missing_or_invalid_secret() -> None:
    with patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "telegram-secret"):
        missing = client.post("/telegram/webhook", json={"message": {}})
        invalid = client.post(
            "/telegram/webhook",
            json={"message": {}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )

    assert missing.status_code == 403
    assert invalid.status_code == 403


def test_telegram_webhook_fails_closed_when_secret_is_unconfigured() -> None:
    with patch.object(main, "TELEGRAM_WEBHOOK_SECRET", None):
        response = client.post("/telegram/webhook", json={"message": {}}, headers=WEBHOOK_HEADERS)

    assert response.status_code == 503


def test_telegram_callback_rejects_non_admin_user() -> None:
    with (
        patch.object(main.telegram_bot, "TELEGRAM_CHAT_ID", "123"),
        patch.object(main, "TELEGRAM_WEBHOOK_SECRET", "telegram-secret"),
        patch.object(main, "TELEGRAM_ADMIN_USER_IDS", {"999"}),
        patch.object(main, "_answer_telegram_callback"),
    ):
        response = client.post(
            "/telegram/webhook",
            json=_callback_payload("approve", "draft-1"),
            headers=WEBHOOK_HEADERS,
        )

    assert response.status_code == 403
