import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.rag import runbook_registry
from core import tasks


def _tool_metadata() -> dict:
    return {
        "name": "check_redis_queue_depth",
        "version": "1.0.0",
        "description": "Read Celery queue depth from Redis broker",
        "risk_level": "read_only",
        "inputs": ["queue_name"],
        "outputs": ["depth"],
        "related_services": ["redis", "celery"],
        "runbook_tags": ["redis-broker", "queue-backlog"],
        "enabled": True,
    }


def test_tool_registry_creates_draft_and_publishes_without_overwriting_existing_runbook() -> None:
    with TemporaryDirectory() as tmp:
        workflow_dir = os.path.join(tmp, "workflow")
        kb_dir = os.path.join(tmp, "knowledge_base")
        os.makedirs(kb_dir)
        original_runbook = os.path.join(kb_dir, "runbook_redis.md")
        with open(original_runbook, "w", encoding="utf-8") as fh:
            fh.write("# Redis runbook\nOriginal content\n")

        with patch.dict(
            os.environ,
            {
                "RUNBOOK_WORKFLOW_DIR": workflow_dir,
                "KNOWLEDGE_BASE_DIR": kb_dir,
            },
        ):
            revision = runbook_registry.save_tool_revision(_tool_metadata(), actor="admin")
            draft = runbook_registry.create_runbook_draft(revision["name"], revision["revision_id"])
            published = runbook_registry.publish_runbook_draft(draft["draft_id"], actor="admin")

        assert draft["status"] == "pending_approval"
        assert published["status"] == "published"
        assert os.path.exists(published["published_path"])
        assert published["published_path"].endswith(".md")

        with open(original_runbook, encoding="utf-8") as fh:
            assert fh.read() == "# Redis runbook\nOriginal content\n"

        current_pointer = os.path.join(kb_dir, "published", draft["runbook_slug"], "current.json")
        assert os.path.exists(current_pointer)


def test_review_tool_change_task_sends_telegram_approval_buttons() -> None:
    with TemporaryDirectory() as tmp:
        workflow_dir = os.path.join(tmp, "workflow")
        kb_dir = os.path.join(tmp, "knowledge_base")
        os.makedirs(kb_dir)

        with patch.dict(
            os.environ,
            {
                "RUNBOOK_WORKFLOW_DIR": workflow_dir,
                "KNOWLEDGE_BASE_DIR": kb_dir,
            },
        ):
            revision = runbook_registry.save_tool_revision(_tool_metadata(), actor="admin")

            with patch.object(tasks, "send_telegram_message", return_value=True) as send:
                result = tasks.review_tool_change_task.run(revision["name"], revision["revision_id"])

        assert result["status"] == "draft_created"
        assert result["notification_sent"] is True
        reply_markup = send.call_args.kwargs["reply_markup"]
        buttons = reply_markup["inline_keyboard"][0]
        assert buttons[0]["callback_data"].startswith("runbook:approve:")
        assert buttons[1]["callback_data"].startswith("runbook:reject:")


def test_runbook_draft_rejects_path_traversal() -> None:
    import pytest
    for bad_id in ["../../evil", "..\\evil", "bad/id", "bad\\id", "id with spaces", ""]:
        with pytest.raises(ValueError):
            runbook_registry.get_runbook_draft(bad_id)
        with pytest.raises(ValueError):
            runbook_registry.update_draft_status(bad_id, "rejected", actor="admin")
        with pytest.raises(ValueError):
            runbook_registry.publish_runbook_draft(bad_id, actor="admin")

