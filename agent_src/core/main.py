# main.py
import os
import logging
import hashlib
import json
import redis
import re
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv

from core.tasks import process_admin_feedback_task, process_alerts_task, review_tool_change_task
from core.event_schema import normalize_alertmanager_payload
from utils.telegram_bot import TELEGRAM_CHAT_ID, send_telegram_message, set_telegram_webhook
from utils import telegram_bot
from core.rag_engine import get_rag_instance
from core.metrics import CELERY_QUEUE_DEPTH, WEBHOOK_EVENTS_TOTAL, get_metrics_response
from core.runbook_registry import (
    get_runbook_draft,
    list_runbook_drafts,
    list_tool_revisions,
    publish_runbook_draft,
    save_tool_revision,
    update_draft_status,
)
from core.tool_auto_discovery import (
    changed_files_from_github_event,
    detect_ci_toolchain,
    github_token,
    register_discovered_ci_toolchain,
    verify_github_signature,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

def valid_env_value(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip()
    placeholders = ("your_", "change_me", "_here")
    if not value or any(marker in value for marker in placeholders):
        return None

    return value


AI_AGENT_PORT       = int(os.getenv("AI_AGENT_PORT", "8000"))
AI_AGENT_PUBLIC_URL = valid_env_value(os.getenv("AI_AGENT_PUBLIC_URL"))
GITHUB_WEBHOOK_SECRET = valid_env_value(os.getenv("GITHUB_WEBHOOK_SECRET"))

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB   = int(os.getenv("REDIS_DB", "0"))
CELERY_QUEUE_NAME = os.getenv("CELERY_QUEUE_NAME", "celery")
CELERY_QUEUE_MAX_LENGTH = int(os.getenv("CELERY_QUEUE_MAX_LENGTH", "1000"))
ALERT_INGRESS_DEDUP_SECONDS = int(os.getenv("ALERT_INGRESS_DEDUP_SECONDS", "60"))


def _configured_telegram_chat_id() -> str | None:
    return telegram_bot.TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID


def _configured_telegram_chat_ids() -> set[str]:
    return {
        str(chat_id)
        for chat_id in (TELEGRAM_CHAT_ID, telegram_bot.TELEGRAM_CHAT_ID)
        if chat_id
    }

# FIX #9: Thêm socket_timeout để tránh treo khi Redis down
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True,
    socket_timeout=5,
    socket_connect_timeout=5,
)

# ─────────────────────────────────────────────
# FIX #4: Dùng lifespan thay cho @app.on_event("startup") (deprecated từ FastAPI 0.93)
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    rag = get_rag_instance()
    if rag is None:
        logger.warning("RAG Engine is unavailable; collections were not initialized.")

    if AI_AGENT_PUBLIC_URL:
        try:
            set_telegram_webhook(AI_AGENT_PUBLIC_URL)
        except Exception as e:
            logger.error(f"Failed to set Telegram webhook: {e}")
    yield
    # (shutdown logic nếu cần đặt ở đây)

app = FastAPI(title="AIOps Intelligent Agent (Celery Enabled)", lifespan=lifespan)


def _alert_identity(alert: dict) -> str:
    fingerprint = alert.get("fingerprint")
    if fingerprint:
        return str(fingerprint)

    labels = alert.get("labels", {})
    identity = {
        "alertname": labels.get("alertname", "Unknown"),
        "instance": labels.get("instance", "Unknown"),
        "job": labels.get("job", ""),
        "service": labels.get("service", ""),
        "target": labels.get("target", ""),
    }
    identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:16]


def _queue_depth() -> int:
    depth = int(redis_client.llen(CELERY_QUEUE_NAME))
    CELERY_QUEUE_DEPTH.set(depth)
    return depth


def _filter_ingress_duplicates(alerts: list[dict]) -> tuple[list[dict], list[str]]:
    accepted = []
    reserved_keys = []

    for alert in alerts:
        if alert.get("status") == "resolved" or ALERT_INGRESS_DEDUP_SECONDS <= 0:
            accepted.append(alert)
            continue

        key = f"alert-ingress-cooldown:{_alert_identity(alert)}"
        reserved = redis_client.set(key, "enqueued", ex=ALERT_INGRESS_DEDUP_SECONDS, nx=True)
        if reserved:
            accepted.append(alert)
            reserved_keys.append(key)
        else:
            WEBHOOK_EVENTS_TOTAL.labels(status="deduped").inc()

    return accepted, reserved_keys

# ─────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────

class Alert(BaseModel):
    status: str
    labels: dict
    annotations: dict
    startsAt: str
    endsAt: Optional[str] = None
    generatorURL: str
    fingerprint: Optional[str] = None

class AlertmanagerPayload(BaseModel):
    alerts: List[Alert]
    status: str


class ToolMetadataRequest(BaseModel):
    name: str
    version: str
    description: str
    risk_level: str
    inputs: list[str] = []
    outputs: list[str] = []
    related_services: list[str] = []
    runbook_tags: list[str] = []
    enabled: bool = True
    actor: str = "admin"


class DraftDecisionRequest(BaseModel):
    actor: str = "admin"
    reason: Optional[str] = None


class TelegramWebhookPayload(BaseModel):
    update_id: Optional[int] = None
    callback_query: Optional[dict] = None
    message: Optional[dict] = None
    edited_message: Optional[dict] = None


def _extract_incident_id(text: str) -> str | None:
    patterns = (
        r"(?:^|\s)/feedback\s+([a-fA-F0-9]{6,16})\b",
        r"(?:^|\s)feedback\s+([a-fA-F0-9]{6,16})\b",
        r"(?:^|\s)ID\s*:\s*([a-fA-F0-9]{6,16})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _extract_feedback_payload(message: dict) -> tuple[str | None, str | None]:
    text = (message.get("text") or message.get("caption") or "").strip()
    reply_text = ((message.get("reply_to_message") or {}).get("text") or "").strip()

    incident_id = _extract_incident_id(text) or _extract_incident_id(reply_text)
    if not incident_id:
        return None, None

    feedback = text
    command_match = re.search(
        r"(?:^|\s)/feedback\s+[a-fA-F0-9]{6,16}\s*(.*)$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    plain_match = re.search(
        r"(?:^|\s)feedback\s+[a-fA-F0-9]{6,16}\s*(.*)$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if command_match:
        feedback = command_match.group(1).strip()
    elif plain_match:
        feedback = plain_match.group(1).strip()

    if _extract_incident_id(text) and feedback == text:
        feedback = re.sub(
            r"(?:^|\s)ID\s*:\s*[a-fA-F0-9]{6,16}\b",
            " ",
            feedback,
            flags=re.IGNORECASE,
        ).strip()

    return incident_id, feedback or None

# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.post("/webhook")
async def prometheus_webhook(payload: AlertmanagerPayload):
    """
    PHASE 3: Tiếp nhận Alert và đẩy ngay vào Celery để xử lý bất đồng bộ.
    
    AlertManager gửi webhook POST → FastAPI nhận → enqueue to Celery
    """
    try:
        depth = _queue_depth()
        payload_dict = normalize_alertmanager_payload(payload.model_dump())
        alerts, reserved_keys = _filter_ingress_duplicates(payload_dict["alerts"])
        if not alerts:
            return {"status": "deduped", "alert_count": 0, "queue_depth": depth}

        if CELERY_QUEUE_MAX_LENGTH > 0 and depth >= CELERY_QUEUE_MAX_LENGTH:
            if reserved_keys:
                redis_client.delete(*reserved_keys)
            WEBHOOK_EVENTS_TOTAL.labels(status="rejected").inc(len(alerts))
            raise HTTPException(status_code=503, detail="Celery queue is at capacity")

        payload_dict["alerts"] = alerts
        try:
            process_alerts_task.delay(payload_dict)
        except Exception:
            if reserved_keys:
                redis_client.delete(*reserved_keys)
            raise

        WEBHOOK_EVENTS_TOTAL.labels(status="enqueued").inc(len(alerts))
        CELERY_QUEUE_DEPTH.set(depth + 1)
        return {"status": "enqueued", "alert_count": len(alerts), "queue_depth": depth + 1}
    except HTTPException:
        raise
    except Exception as e:
        WEBHOOK_EVENTS_TOTAL.labels(status="error").inc(len(payload.alerts))
        logger.error("Failed to enqueue webhook alerts: %s", e)
        raise HTTPException(status_code=503, detail="Redis queue is unavailable") from e


@app.get("/metrics")
async def metrics():
    try:
        _queue_depth()
    except redis.RedisError:
        logger.warning("Unable to refresh Celery queue depth metric")
    return get_metrics_response()


@app.post("/github/webhook", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: Optional[str] = Header(default=None),
    x_hub_signature_256: Optional[str] = Header(default=None),
):
    """Auto-discover CI/IaC tooling changes from GitHub push or PR webhooks."""
    body = await request.body()
    if not verify_github_signature(GITHUB_WEBHOOK_SECRET, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e

    event = x_github_event or payload.get("event") or ""
    if event not in {"push", "pull_request"}:
        return {"status": "ignored", "reason": f"unsupported event: {event or 'unknown'}"}

    try:
        files = changed_files_from_github_event(event, payload, token=github_token())
    except requests.RequestException as e:
        logger.error("Failed to fetch GitHub changed files: %s", e)
        raise HTTPException(status_code=502, detail="Unable to fetch GitHub changed files") from e

    detection = detect_ci_toolchain(files)
    if not detection:
        return {"status": "ignored", "reason": "no watched CI toolchain change", "files_checked": len(files)}

    actor = "github-webhook"
    sender = payload.get("sender") or {}
    if sender.get("login"):
        actor = f"github:{sender['login']}"

    result = register_discovered_ci_toolchain(payload, detection, actor=actor)
    return {
        **result,
        "discovered_tools": detection["discovered_tools"],
        "changed_files": detection["changed_files"],
    }


@app.post("/api/tools", status_code=202)
async def register_tool(metadata: ToolMetadataRequest):
    """Register a tool revision and enqueue runbook draft review."""
    try:
        revision = save_tool_revision(metadata.model_dump(exclude={"actor"}), actor=metadata.actor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    review_status = "queued"
    try:
        review_tool_change_task.delay(revision["name"], revision["revision_id"])
    except Exception as e:
        review_status = "queue_unavailable"
        logger.error("Failed to enqueue tool change review: %s", e)

    return {
        "status": "registered",
        "review_status": review_status,
        "tool_name": revision["name"],
        "revision_id": revision["revision_id"],
    }


@app.get("/api/tools")
async def list_tools():
    return {"tools": list_tool_revisions()}


@app.get("/api/runbook-drafts")
async def get_runbook_drafts(status: Optional[str] = None):
    return {"drafts": list_runbook_drafts(status=status)}


@app.get("/api/runbook-drafts/{draft_id}")
async def get_runbook_draft_by_id(draft_id: str):
    try:
        return get_runbook_draft(draft_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="Runbook draft not found") from e


@app.post("/api/runbook-drafts/{draft_id}/approve")
async def approve_runbook_draft(draft_id: str, decision: DraftDecisionRequest):
    try:
        draft = publish_runbook_draft(draft_id, actor=decision.actor)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="Runbook draft not found") from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    rag = get_rag_instance()
    if rag and draft.get("published_path"):
        rag.ingest_runbook_file(draft["published_path"])

    return {"status": "published", "draft": draft}


@app.post("/api/runbook-drafts/{draft_id}/reject")
async def reject_runbook_draft(draft_id: str, decision: DraftDecisionRequest):
    try:
        draft = update_draft_status(draft_id, "rejected", actor=decision.actor, reason=decision.reason)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="Runbook draft not found") from e
    return {"status": "rejected", "draft": draft}


def _answer_telegram_callback(callback_query_id: str, text: str) -> None:
    if not telegram_bot.TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{telegram_bot.TELEGRAM_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=5)
    except requests.RequestException as e:
        logger.warning("Failed to answer Telegram callback: %s", e)


def _telegram_callback_chat_allowed(callback_query: dict) -> bool:
    expected = _configured_telegram_chat_ids()
    if not expected:
        return False
    chat = callback_query.get("message", {}).get("chat", {})
    chat_id = chat.get("id")
    return str(chat_id) in expected


def _publish_draft_from_callback(draft_id: str, actor: str) -> dict:
    draft = publish_runbook_draft(draft_id, actor=actor)
    rag = get_rag_instance()
    if rag and draft.get("published_path"):
        rag.ingest_runbook_file(draft["published_path"])
    return draft


@app.post("/telegram/webhook")
async def telegram_webhook(payload: TelegramWebhookPayload):
    callback_query = payload.callback_query
    if not callback_query:
        message = payload.message or payload.edited_message or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        expected_chat_ids = _configured_telegram_chat_ids()

        if not expected_chat_ids or str(chat_id) not in expected_chat_ids:
            logger.warning("Ignored Telegram message from unauthorized chat_id=%s", chat_id)
            return {"status": "ignored"}

        incident_id, feedback = _extract_feedback_payload(message)
        if not incident_id or not feedback:
            send_telegram_message(
                "Gửi góp ý theo cú pháp: /feedback <incident_id> <giải pháp>",
                chat_id=str(chat_id) if chat_id is not None else None,
                parse_mode=None,
            )
            return {"status": "ignored", "reason": "missing_feedback"}

        process_admin_feedback_task.delay(
            incident_id,
            feedback,
            str(chat_id) if chat_id is not None else None,
        )
        return {"status": "enqueued", "incident_id": incident_id}

    callback_id = callback_query.get("id", "")
    if not _telegram_callback_chat_allowed(callback_query):
        if callback_id:
            _answer_telegram_callback(callback_id, "Unauthorized chat")
        raise HTTPException(status_code=403, detail="Unauthorized Telegram chat")

    data = str(callback_query.get("data", ""))
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "runbook" or parts[1] not in {"approve", "reject"}:
        if callback_id:
            _answer_telegram_callback(callback_id, "Unknown action")
        return {"status": "ignored"}

    action = parts[1]
    draft_id = parts[2]
    user = callback_query.get("from", {})
    actor = user.get("username") or user.get("first_name") or "telegram-admin"

    try:
        if action == "approve":
            draft = _publish_draft_from_callback(draft_id, actor=actor)
            text = f"Published runbook draft {draft_id}"
            status = "published"
        else:
            draft = update_draft_status(draft_id, "rejected", actor=actor, reason="Rejected from Telegram")
            text = f"Rejected runbook draft {draft_id}"
            status = "rejected"
    except FileNotFoundError as e:
        if callback_id:
            _answer_telegram_callback(callback_id, "Draft not found")
        raise HTTPException(status_code=404, detail="Runbook draft not found") from e
    except ValueError as e:
        if callback_id:
            _answer_telegram_callback(callback_id, str(e))
        raise HTTPException(status_code=409, detail=str(e)) from e

    if callback_id:
        _answer_telegram_callback(callback_id, text)
    send_telegram_message(f"{text}\nActor: {actor}", parse_mode=None)
    return {"status": status, "draft": draft}


@app.get("/health")
async def health():
    # Kiểm tra Redis health
    try:
        redis_client.ping()
        redis_status = "connected"
        queue_depth = _queue_depth()
    except redis.RedisError:
        redis_status = "disconnected"
        queue_depth = None

    status = "healthy" if redis_status == "connected" else "degraded"
    status_code = 200 if redis_status == "connected" else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "queue": "celery-redis",
            "queue_depth": queue_depth,
            "queue_capacity": CELERY_QUEUE_MAX_LENGTH,
            "redis": redis_status,
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=AI_AGENT_PORT)
