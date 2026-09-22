# tasks.py
# FIX #5: Sắp xếp lại imports — stdlib trước, third-party sau, local cuối cùng
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
from core.celery_app import celery_app
from core.metrics import (
    ACTIVE_TASKS,
    AI_WORKFLOW_LATENCY_SECONDS,
    ALERTS_PROCESSED_TOTAL,
)
from core.rag_engine import get_rag_instance
from core.runbook_registry import create_runbook_draft
from core.shadow_pipeline import run_shadow_if_enabled
from dotenv import load_dotenv
from google import genai
from google.genai import types
from tools.diag_tools import AGENT_TOOLS
from tools.prometheus_check import get_prometheus_checker
from utils.telegram_bot import send_telegram_message

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

def valid_env_value(value):
    if not value:
        return None

    value = value.strip()
    placeholders = ("your_", "change_me", "_here")
    if not value or any(marker in value for marker in placeholders):
        return None

    return value


GEMINI_API_KEY = valid_env_value(os.getenv("GEMINI_API_KEY"))
GEMINI_MODEL = valid_env_value(os.getenv("GEMINI_MODEL")) or "gemini-2.5-flash"
GEMINI_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
    if model.strip()
]
GEMINI_MAX_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "1"))
GEMINI_MAX_REMOTE_CALLS = int(os.getenv("GEMINI_MAX_REMOTE_CALLS", "1"))
GEMINI_RETRY_BASE_SECONDS = float(os.getenv("GEMINI_RETRY_BASE_SECONDS", "2"))
ALERT_AI_COOLDOWN_SECONDS = int(os.getenv("ALERT_AI_COOLDOWN_SECONDS", "900"))
ALERT_NOTIFICATION_TTL_SECONDS = int(os.getenv("ALERT_NOTIFICATION_TTL_SECONDS", "86400"))
ALERT_DEDUP_ENABLED = os.getenv("ALERT_DEDUP_ENABLED", "true").lower() not in {"0", "false", "no"}
ALERT_BATCH_CONCURRENCY = max(int(os.getenv("ALERT_BATCH_CONCURRENCY", "4")), 1)
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Redis Configuration (để lưu incident context)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB   = int(os.getenv("REDIS_DB", "0"))

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True,
    socket_timeout=5,
    socket_connect_timeout=5,
)

_local_alert_cooldowns: dict[str, float] = {}
_local_alert_notifications: dict[str, float] = {}


def _truncate_text(value: str, max_chars: int = 1500) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "\n...[truncated]"


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


def _alert_cooldown_key(alert: dict) -> str:
    return f"alert-ai-cooldown:{_alert_identity(alert)}"


def _alert_event_hash(alert: dict) -> str:
    event_start = str(alert.get("startsAt") or "unknown-start")
    return hashlib.sha256(event_start.encode("utf-8")).hexdigest()[:12]


def _alert_notification_key(alert: dict, notification_type: str) -> str:
    return f"alert-ai-notification:{notification_type}:{_alert_identity(alert)}:{_alert_event_hash(alert)}"


def _active_incident_key(alert: dict) -> str:
    return f"alert-ai-active-incident:{_alert_identity(alert)}:{_alert_event_hash(alert)}"


def _reserve_alert_notification(alert: dict, notification_type: str) -> bool:
    if not ALERT_DEDUP_ENABLED or ALERT_NOTIFICATION_TTL_SECONDS <= 0:
        return True

    key = _alert_notification_key(alert, notification_type)
    if redis_client is not None:
        try:
            return bool(redis_client.set(key, "sent", ex=ALERT_NOTIFICATION_TTL_SECONDS, nx=True))
        except redis.RedisError as e:
            logger.warning("Redis notification dedup failed, using in-memory fallback: %s", e)

    now = time.time()
    expires_at = _local_alert_notifications.get(key, 0)
    if expires_at > now:
        return False
    _local_alert_notifications[key] = now + ALERT_NOTIFICATION_TTL_SECONDS
    return True


def _reserve_alert_processing(alert: dict) -> bool:
    if not ALERT_DEDUP_ENABLED or ALERT_AI_COOLDOWN_SECONDS <= 0:
        return True

    key = _alert_cooldown_key(alert)
    if redis_client is not None:
        try:
            return bool(redis_client.set(key, "processing", ex=ALERT_AI_COOLDOWN_SECONDS, nx=True))
        except redis.RedisError as e:
            logger.warning("Redis cooldown check failed, using in-memory fallback: %s", e)

    now = time.time()
    expires_at = _local_alert_cooldowns.get(key, 0)
    if expires_at > now:
        return False
    _local_alert_cooldowns[key] = now + ALERT_AI_COOLDOWN_SECONDS
    return True


def _clear_alert_cooldown(alert: dict) -> None:
    key = _alert_cooldown_key(alert)
    _local_alert_cooldowns.pop(key, None)
    if redis_client is None:
        return
    try:
        redis_client.delete(key)
    except redis.RedisError as e:
        logger.warning("Redis cooldown cleanup failed: %s", e)


def _format_labels(labels: dict) -> str:
    if not labels:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(labels.items()))


def _format_annotations(annotations: dict) -> str:
    if not annotations:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(annotations.items()))


def build_incident_details(alert: dict) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alert_name = labels.get("alertname", "Unknown")
    instance = labels.get("instance", "Unknown")
    job = labels.get("job", "unknown")
    service = labels.get("service", "unknown")
    target = labels.get("target", labels.get("endpoint", "unknown"))
    summary = annotations.get("summary", "")
    description = annotations.get("description", "")
    generator_url = alert.get("generatorURL", "")

    return (
        f"Alert: {alert_name}\n"
        f"Instance: {instance}\n"
        f"Job: {job}\n"
        f"Service: {service}\n"
        f"Target: {target}\n"
        f"Summary: {summary}\n"
        f"Description: {description}\n"
        f"GeneratorURL: {generator_url}\n"
        f"Labels: {_format_labels(labels)}\n"
        f"Annotations: {_format_annotations(annotations)}"
    )


def _alert_environment(labels: dict, target: str = "") -> str:
    environment = labels.get("environment", "")
    component = labels.get("component", "")
    if environment:
        return environment
    if component.endswith("-staging") or ":180" in target or ":191" in target:
        return "staging"
    return "production"


def _default_component(labels: dict, environment: str, prod_name: str, staging_name: str) -> str:
    component = labels.get("component")
    if component:
        return component
    return staging_name if environment == "staging" else prod_name


def _deploy_role_for_component(component: str) -> str:
    if component.startswith("frontend-web"):
        return "web"
    if component.startswith(("payment-api", "postgres")):
        return "core"
    return "monitor"


def _action_component(component: str) -> str:
    return component.replace("-", "_").replace(".", "_")


RUNBOOK_FILES_BY_ALERT = {
    "WebEndpointDown": "runbook_nginx.md",
    "FrontendAPIProxyDown": "runbook_nginx.md",
    "PaymentAPIEndpointDown": "runbook_postgresql.md",
    "PostgreSQLDown": "runbook_postgresql.md",
    "RedisDown": "runbook_redis.md",
    "RedisBrokerDown": "runbook_redis.md",
    "DockerContainerDown": "runbook_docker.md",
}


def _knowledge_base_path(filename: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "config", "knowledge_base", filename)


def _load_runbook_section(filename: str, alert_name: str) -> str:
    path = _knowledge_base_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as file:
            content = file.read()
    except OSError as exc:
        logger.warning("Unable to read runbook file %s: %s", path, exc)
        return ""

    section_pattern = rf"^##\s+{re.escape(alert_name)}\s*$"
    match = re.search(section_pattern, content, flags=re.MULTILINE)
    if not match:
        return content.strip()

    next_match = re.search(r"^##\s+", content[match.end():], flags=re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(content)
    return content[match.start():end].strip()


def _render_runbook_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered.strip()


def _diagnosis_values(alert: dict) -> dict[str, str]:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alert_name = labels.get("alertname", "Unknown")
    instance = labels.get("instance", "Unknown")
    target = labels.get("target", "unknown")
    summary = annotations.get("summary", "")
    environment = _alert_environment(labels, target)
    state_file = f"release/.state/{environment}.tag"

    component = labels.get("component", "unknown")
    role = _deploy_role_for_component(component)
    if alert_name in {"WebEndpointDown", "FrontendAPIProxyDown"}:
        component = _default_component(labels, environment, "frontend-web-prod", "frontend-web-staging")
        role = "web"
    elif alert_name == "PaymentAPIEndpointDown":
        component = _default_component(labels, environment, "payment-api-prod", "payment-api-staging")
        role = "core"
    elif alert_name == "PostgreSQLDown":
        component = _default_component(labels, environment, "postgres-prod", "postgres-staging")
        role = "core"
    elif alert_name in {"RedisDown", "RedisBrokerDown"}:
        prod_name = "redis-prod" if alert_name == "RedisBrokerDown" else "redis-cache-prod"
        staging_name = "redis-staging" if alert_name == "RedisBrokerDown" else "redis-cache-staging"
        component = _default_component(labels, environment, prod_name, staging_name)
        role = "monitor"
    elif alert_name == "DockerContainerDown":
        component = labels.get("component", "unknown-container")
        role = _deploy_role_for_component(component)

    return {
        "alert_name": alert_name,
        "instance": instance,
        "target": target,
        "summary": summary or "không có",
        "environment": environment,
        "component": component,
        "role": role,
        "dependency": labels.get("dependency", "payment-api"),
        "expected_upstream": labels.get("expected_upstream", "http://<core-private-ip>:18080"),
        "state_file": state_file,
        "local_web_health_url": "http://127.0.0.1:18081/health" if environment == "staging" else "http://127.0.0.1:3000/health",
        "local_web_api_health_url": "http://127.0.0.1:18081/api/health" if environment == "staging" else "http://127.0.0.1:3000/api/health",
        "local_proxy_ready_url": "http://127.0.0.1:18081/api/ready" if environment == "staging" else "http://127.0.0.1:3000/api/ready",
        "local_payment_ready_url": "http://127.0.0.1:18080/api/ready" if environment == "staging" else "http://127.0.0.1:8080/api/ready",
        "local_payment_health_url": "http://127.0.0.1:18080/api/health" if environment == "staging" else "http://127.0.0.1:8080/api/health",
        "redis_exporter_url": "http://127.0.0.1:19121/metrics" if environment == "staging" else "http://127.0.0.1:9121/metrics",
        "deploy_command": f'./automation/app-release-deploy.sh {environment} "$TAG" {role}',
    }


def _proposal_for_alert(alert_name: str, values: dict[str, str]) -> dict | None:
    component = values["component"]
    instance = values["instance"]
    if alert_name in {"WebEndpointDown", "PostgreSQLDown", "RedisDown", "RedisBrokerDown"}:
        return {"action": f"check_or_start_{_action_component(component)}", "host": instance}
    if alert_name == "FrontendAPIProxyDown":
        return {"action": f"fix_{_action_component(component)}_api_upstream", "host": instance}
    if alert_name == "PaymentAPIEndpointDown":
        return {"action": f"restore_{_action_component(component)}_endpoint", "host": instance}
    if alert_name == "DockerContainerDown":
        return {"action": f"redeploy_{values['role']}_{values['environment']}", "host": instance}
    return None


def deterministic_diagnosis(alert: dict) -> tuple[str, dict | None]:
    labels = alert.get("labels", {})
    alert_name = labels.get("alertname", "Unknown")
    values = _diagnosis_values(alert)
    filename = RUNBOOK_FILES_BY_ALERT.get(alert_name)
    if filename:
        section = _load_runbook_section(filename, alert_name)
        if section:
            return _render_runbook_template(section, values), _proposal_for_alert(alert_name, values)

    resource_alerts = {
        "HighCPUUsage": ("CPU", "80", "top -o %CPU", "ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%cpu | head"),
        "CriticalCPUUsage": ("CPU", "95", "top -o %CPU", "ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%cpu | head"),
        "HighMemoryUsage": ("memory", "85", "free -m", "ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%mem | head"),
        "CriticalMemoryUsage": ("memory", "95", "free -m", "ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%mem | head"),
        "HighDiskUsage": ("disk", "80", "df -h /", "du -xhd1 /var /home 2>/dev/null | sort -h"),
        "CriticalDiskUsage": ("disk", "90", "df -h /", "du -xhd1 /var /home 2>/dev/null | sort -h"),
    }
    if alert_name in resource_alerts:
        resource, threshold, primary_check, process_check = resource_alerts[alert_name]
        instance = values["instance"]
        summary = values["summary"]
        analysis = (
            f"Chẩn đoán: host vượt ngưỡng {resource} {threshold}%.\n"
            f"Host: {instance}\n"
            f"Tóm tắt: {summary}\n\n"
            "Nguyên nhân ưu tiên:\n"
            "1. Tiến trình ứng dụng hoặc tác vụ demo đang tiêu thụ tài nguyên bất thường.\n"
            "2. Container restart loop, truy vấn nặng hoặc log tăng nhanh.\n"
            "3. Host thiếu capacity so với tải hiện tại.\n\n"
            f"Kiểm tra trên {instance}:\n"
            f"{primary_check}\n"
            f"{process_check}\n"
            "docker stats --no-stream\n\n"
            "Khôi phục:\n"
            "Dừng tác vụ gây tải, sửa tiến trình bất thường hoặc scale host trước khi restart dịch vụ."
        )
        return analysis, {"action": f"reduce_{resource.lower()}_usage", "host": instance}

    return "", None


def _alert_report_component(alert: dict) -> str:
    values = _diagnosis_values(alert)
    return values.get("component", "unknown")


def _alert_report_role(alert: dict) -> str:
    values = _diagnosis_values(alert)
    return values.get("role", "monitor")


def _incident_field(details: str, field_name: str, default: str = "unknown") -> str:
    match = re.search(rf"^{re.escape(field_name)}:\s*(.+)$", details, flags=re.MULTILINE)
    return match.group(1).strip() if match else default


def _labels_from_incident_context(ctx: dict) -> dict:
    labels = ctx.get("labels")
    if isinstance(labels, dict):
        return labels

    details = str(ctx.get("incident_details", ""))
    labels_line = _incident_field(details, "Labels", "")
    parsed = {}
    for item in labels_line.split(", "):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _format_alert_report(alert: dict, incident_id: str, proposal: dict | None, ai_analysis: str) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alert_name = labels.get("alertname", "Unknown")
    instance = labels.get("instance", "Unknown")
    target = labels.get("target", labels.get("endpoint", "unknown"))
    summary = annotations.get("summary", "")
    action_name = proposal.get("action", "manual_fix") if proposal else "manual_fix"
    component = _alert_report_component(alert)

    return (
        f"🚨 Sự cố: {alert_name}\n"
        f"ID: {incident_id}\n"
        f"Host: {instance}\n"
        f"Component: {component}\n"
        f"Target: {target}\n"
        f"Action: {action_name}\n"
        f"Tóm tắt: {summary or 'không có'}\n\n"
        f"✅ Runbook xử lý\n"
        f"{_truncate_text(ai_analysis, 3500)}\n\n"
        "Agent sẽ tự kiểm tra lại sau 5 phút.\n"
        f"Feedback: /feedback {incident_id} <góp ý>"
    )


def _compact_review_text(value: str, max_chars: int = 900) -> str:
    cleaned = re.sub(r"REVIEW_JSON:\s*\{.*\}", "", value, flags=re.DOTALL).strip()
    lines = [line.strip(" *") for line in cleaned.splitlines() if line.strip()]
    compact = "\n".join(lines[:8]) if lines else cleaned
    return _truncate_text(compact, max_chars)


def _format_admin_feedback_response(
    incident_id: str,
    review_status: str,
    saved: bool,
    admin_message: str,
    reviewed_solution: str,
) -> str:
    status_labels = {
        "accepted": "accepted - dùng được",
        "revised": "revised - đã chỉnh cho an toàn hơn",
        "rejected": "rejected - không nên làm",
    }
    save_label = "yes" if saved else "no"
    return (
        "📝 Feedback reviewed\n"
        f"Incident: {incident_id}\n"
        f"Kết quả: {status_labels.get(review_status, review_status)}\n"
        f"Lưu vào RAG: {save_label}\n\n"
        f"Nhận xét ngắn:\n{_compact_review_text(admin_message, 500)}\n\n"
        f"✅ Làm theo:\n{_compact_review_text(reviewed_solution, 900)}"
    )


def save_incident_to_redis(incident_id: str, context: dict, ttl: int = 86400):
    if redis_client is None:
        logger.error("Redis client unavailable, skipping incident save.")
        return
    try:
        redis_client.setex(f"incident:{incident_id}", ttl, json.dumps(context))
    except redis.RedisError as e:
        logger.error(f"Error writing to Redis: {e}")


def _runbook_approval_keyboard(draft_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Duyệt", "callback_data": f"runbook:approve:{draft_id}"},
                {"text": "Từ chối", "callback_data": f"runbook:reject:{draft_id}"},
            ]
        ]
    }


def notify_runbook_draft_for_approval(draft: dict) -> bool:
    content_preview = _truncate_text(draft.get("content", ""), 1200)
    message = (
        "RUNBOOK DRAFT CAN DUYET\n"
        f"Draft ID: {draft['draft_id']}\n"
        f"Tool: {draft['tool_name']}\n"
        f"Change type: {draft['change_type']}\n"
        f"Runbook slug: {draft['runbook_slug']}\n\n"
        "Preview:\n"
        f"{content_preview}\n\n"
        "Bam Duyet de publish version moi, hoac Tu choi de giu nguyen runbook hien tai."
    )
    return bool(send_telegram_message(message, reply_markup=_runbook_approval_keyboard(draft["draft_id"]), parse_mode=None))


def _load_incident_from_redis(incident_id: str) -> dict | None:
    if redis_client is None:
        logger.error("Redis client unavailable, cannot load incident context.")
        return None
    try:
        ctx_raw = redis_client.get(f"incident:{incident_id}")
    except redis.RedisError as e:
        logger.error("Redis read error for incident %s: %s", incident_id, e)
        return None
    if not ctx_raw:
        return None
    try:
        return json.loads(ctx_raw)
    except json.JSONDecodeError:
        logger.error("Invalid incident context JSON for incident %s", incident_id)
        return None


def _link_active_incident(alert: dict, incident_id: str, ttl: int = 86400) -> None:
    if redis_client is None:
        return
    try:
        redis_client.setex(_active_incident_key(alert), ttl, incident_id)
    except redis.RedisError as e:
        logger.warning("Unable to link active incident %s: %s", incident_id, e)


def _mark_matching_incident_recovery_signal(alert: dict) -> str | None:
    """Record Alertmanager recovery without declaring the incident resolved.

    A resolved notification is only a recovery signal. The scheduled verifier
    still has to check current metrics before the incident can be closed.
    """
    if redis_client is None:
        return None

    active_key = _active_incident_key(alert)
    try:
        incident_id = redis_client.get(active_key)
        if not incident_id:
            return None

        context = _load_incident_from_redis(incident_id)
        if context is not None:
            context["status"] = "verification_pending"
            context["recovery_signal_at"] = str(
                alert.get("endsAt") or datetime.now(VN_TZ).isoformat()
            )
            context["recovery_signal_source"] = "alertmanager"
            save_incident_to_redis(incident_id, context)

        return incident_id
    except redis.RedisError as e:
        logger.warning("Unable to record matching incident recovery signal: %s", e)
        return None


def _parse_review_json(full_text: str) -> dict | None:
    match = re.search(r"REVIEW_JSON:\s*(\{.*\})", full_text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("Failed to parse REVIEW_JSON from AI response.")
        return None


def _destructive_feedback_review(admin_feedback: str) -> dict | None:
    feedback_lower = admin_feedback.lower()
    destructive_markers = (
        "xóa docker volume",
        "xoa docker volume",
        "docker volume rm",
        "docker system prune --volumes",
        "drop database",
        "drop table",
        "truncate table",
        "rm -rf /var/lib",
        "rm -rf /data",
        "xóa dữ liệu",
        "xoa du lieu",
    )
    if not any(marker in feedback_lower for marker in destructive_markers):
        return None

    return {
        "status": "rejected",
        "reviewed_solution": (
            "Không thực hiện thao tác xóa dữ liệu hoặc Docker volume.\n"
            "1. Kiểm tra trạng thái container và logs.\n"
            "2. Thử start/restart hoặc redeploy đúng role.\n"
            "3. Chỉ xóa dữ liệu khi có backup, xác nhận phạm vi ảnh hưởng và phê duyệt rõ ràng."
        ),
        "admin_message": (
            "Góp ý chứa thao tác phá dữ liệu nhưng chưa có backup hoặc xác nhận rõ ràng. "
            "Agent từ chối lưu góp ý này vào RAG."
        ),
    }


def _configuration_feedback_review(incident_context: dict, admin_feedback: str) -> dict | None:
    if incident_context.get("alert_name") != "FrontendAPIProxyDown":
        return None

    labels = _labels_from_incident_context(incident_context)
    expected_upstream = str(labels.get("expected_upstream", "")).strip()
    feedback_lower = admin_feedback.lower()
    if not expected_upstream or "payment_api_upstream" not in feedback_lower:
        return None
    if expected_upstream.lower() in feedback_lower:
        return None

    return {
        "status": "revised",
        "reviewed_solution": (
            "cd /home/ec2-user/aws-hybrid\n"
            "grep '^PAYMENT_API_UPSTREAM=' release/.env.staging\n"
            f"sed -i 's|^PAYMENT_API_UPSTREAM=.*|PAYMENT_API_UPSTREAM={expected_upstream}|' release/.env.staging\n"
            "TAG=$(cat release/.state/staging.tag)\n"
            "./automation/app-release-deploy.sh staging \"$TAG\" web\n"
            "curl -i http://127.0.0.1:18081/api/ready"
        ),
        "admin_message": (
            "Góp ý đúng hướng nhưng chưa nêu giá trị PAYMENT_API_UPSTREAM cần khôi phục. "
            "Agent đã bổ sung upstream mong đợi từ alert context."
        ),
    }


def _basic_feedback_review(admin_feedback: str) -> dict:
    destructive_review = _destructive_feedback_review(admin_feedback)
    if destructive_review:
        return destructive_review

    feedback = admin_feedback.strip()
    action_markers = (
        "check",
        "restart",
        "start",
        "docker",
        "curl",
        "logs",
        "rollback",
        "redeploy",
        "kiểm tra",
        "khoi phuc",
        "khôi phục",
        "khởi động",
        "xem log",
    )
    has_action = any(marker in feedback.lower() for marker in action_markers)
    if len(feedback) >= 20 and has_action:
        return {
            "status": "accepted",
            "reviewed_solution": feedback,
            "admin_message": (
                "Agent đã kiểm tra cơ bản và ghi nhận góp ý này vào RAG. "
                "Khi có Gemini, Agent sẽ đánh giá sâu hơn theo ngữ cảnh incident."
            ),
        }

    return {
        "status": "revised",
        "reviewed_solution": (
            "Góp ý của admin chưa đủ chi tiết để lưu trực tiếp. "
            f"Nội dung gốc: {feedback}\n"
            "Bản chỉnh: xác nhận alert còn firing, kiểm tra log/service liên quan, "
            "thực hiện thay đổi nhỏ nhất để khôi phục, rồi verify lại metric/health endpoint."
        ),
        "admin_message": "Góp ý chưa đủ hành động cụ thể, Agent đã chỉnh lại thành checklist an toàn hơn.",
    }


async def review_admin_feedback(incident_context: dict, admin_feedback: str) -> dict:
    destructive_review = _destructive_feedback_review(admin_feedback)
    if destructive_review:
        return destructive_review

    configuration_review = _configuration_feedback_review(incident_context, admin_feedback)
    if configuration_review:
        return configuration_review

    if not GEMINI_API_KEY:
        return _basic_feedback_review(admin_feedback)

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
Bạn là AI Ops Agent. Hãy đánh giá góp ý xử lý sự cố của admin.

Incident context:
{incident_context.get("incident_details", "")}

Phân tích ban đầu của Agent:
{incident_context.get("ai_analysis", "")}

Ngữ cảnh RAG liên quan:
{incident_context.get("rag_context", "")}

Góp ý của admin:
{admin_feedback}

Yêu cầu:
- Nếu góp ý đúng, an toàn và hữu ích: status = "accepted".
- Nếu góp ý có ý đúng nhưng thiếu bước/thiếu an toàn: status = "revised" và viết lại giải pháp tốt hơn.
- Nếu góp ý sai hoặc rủi ro: status = "rejected" và đưa giải pháp thay thế an toàn.
- Bất kỳ góp ý nào chứa thao tác phá dữ liệu mà chưa có backup/xác nhận rõ ràng đều bắt buộc status = "rejected", kể cả khi có phần khác hợp lý.
- admin_message tối đa 2 câu, nói thẳng vì sao accepted/revised/rejected.
- reviewed_solution tối đa 5 dòng, ưu tiên lệnh cần chạy ngay.
- Dòng cuối cùng bắt buộc là:
REVIEW_JSON: {{"status": "accepted|revised|rejected", "reviewed_solution": "...", "admin_message": "..."}}
"""
    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=0
                )
            )
        )
        full_text = response.text or ""
        parsed = _parse_review_json(full_text)
        if parsed:
            return {
                "status": parsed.get("status", "revised"),
                "reviewed_solution": parsed.get("reviewed_solution", "").strip() or full_text.strip(),
                "admin_message": parsed.get("admin_message", "").strip() or "Agent đã đánh giá góp ý.",
            }
        return {
            "status": "revised",
            "reviewed_solution": full_text.strip() or admin_feedback.strip(),
            "admin_message": "Agent đã đánh giá góp ý nhưng phản hồi AI không đúng định dạng JSON.",
        }
    except Exception as e:  # noqa: BLE001 - external model failures use deterministic fallback
        logger.warning("Gemini feedback review failed, using basic review: %s", e)
        return _basic_feedback_review(admin_feedback)


async def process_admin_feedback(incident_id: str, admin_feedback: str, chat_id: str | None = None) -> dict:
    incident_id = incident_id.strip()
    admin_feedback = admin_feedback.strip()
    if not incident_id or not admin_feedback:
        result = {"status": "invalid", "message": "Thiếu incident ID hoặc nội dung góp ý."}
        send_telegram_message(result["message"], chat_id=chat_id, parse_mode=None)
        return result

    ctx = _load_incident_from_redis(incident_id)
    if not ctx:
        message = (
            f"Không tìm thấy context cho incident `{incident_id}`. "
            "Hãy gửi góp ý khi incident còn trong TTL Redis hoặc kiểm tra lại ID."
        )
        send_telegram_message(message, chat_id=chat_id, parse_mode=None)
        return {"status": "not_found", "message": message}

    review = await review_admin_feedback(ctx, admin_feedback)
    review_status = str(review.get("status", "revised"))
    reviewed_solution = str(review.get("reviewed_solution", admin_feedback)).strip()

    rag = get_rag_instance()
    saved = False
    if rag and review_status in {"accepted", "revised"}:
        rag.save_admin_solution(
            incident_id=incident_id,
            alert_name=ctx.get("alert_name", "Unknown"),
            incident_details=ctx.get("incident_details", ""),
            admin_feedback=admin_feedback,
            reviewed_solution=reviewed_solution,
            review_status=review_status,
        )
        saved = True

    message = _format_admin_feedback_response(
        incident_id=incident_id,
        review_status=review_status,
        saved=saved,
        admin_message=str(review.get("admin_message", "")),
        reviewed_solution=reviewed_solution,
    )
    send_telegram_message(message, chat_id=chat_id, parse_mode=None)
    return {"status": review_status, "saved": saved, "message": message}


async def run_agent_workflow(incident_details: str, alert_name: str | None = None):
    if not GEMINI_API_KEY:
        return "❌ Error: GEMINI_API_KEY not configured", None

    client = genai.Client(api_key=GEMINI_API_KEY)
    rag = get_rag_instance()

    runbook_context = "⚠️ RAG Engine không khả dụng."
    if rag:
        runbook_context = rag.query_knowledge(incident_details, alert_name=alert_name)

    system_instruction = f"""
        Bạn là AI Ops Agent chuyên nghiệp, chuyên xử lý sự cố hạ tầng.
        QUY TRÌNH CHUẨN VÀ LỊCH SỬ INCIDENT từ kho tri thức:
        ---
        {runbook_context}
        ---
        BẮT BUỘC: Dòng cuối cùng của response PHẢI là:
        PROPOSAL_JSON: {{"action": "tên_hành_động", "host": "tên_máy_chủ"}}
    """

    def parse_proposal(full_text: str):
        match = re.search(r"PROPOSAL_JSON:\s*(\{.*\})", full_text)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning("Failed to parse PROPOSAL_JSON from AI response.")
            return None

    def is_retryable_gemini_error(exc: Exception) -> bool:
        error_text = str(exc).lower()
        retryable_markers = (
            "429",
            "500",
            "502",
            "503",
            "504",
            "deadline",
            "rate limit",
            "resource_exhausted",
            "temporarily",
            "timeout",
            "unavailable",
            "high demand",
        )
        return any(marker in error_text for marker in retryable_markers)

    def fallback_analysis(last_error: Exception | None):
        host_match = re.search(r"(?:Host|Instance):\s*([^\n|]+)", incident_details)
        host = host_match.group(1).strip() if host_match else "unknown"
        error_text = _truncate_text(str(last_error), 500) if last_error else "Gemini không phản hồi."
        analysis = (
            "⚠️ *Gemini tạm thời không khả dụng, dùng phân tích dự phòng từ RAG/runbook.*\n\n"
            f"*Sự cố:* {incident_details}\n\n"
            f"*Lỗi Gemini gần nhất:* `{error_text}`\n\n"
            "*Ngữ cảnh runbook/RAG liên quan:*\n"
            f"{_truncate_text(runbook_context, 1200)}\n\n"
            "*Biện pháp khắc phục đề xuất:*\n"
            "1. Xác nhận alert còn firing trong Prometheus/Alertmanager.\n"
            "2. Kiểm tra service/endpoint bị báo lỗi trên host liên quan.\n"
            "3. Khôi phục service hoặc rollback release gần nhất nếu lỗi xuất hiện sau deploy.\n"
            "4. Kiểm tra lại `/health` và chờ Prometheus resolve alert.\n"
            "PROPOSAL_JSON: "
            + json.dumps({"action": "fallback_runbook_recovery", "host": host}, ensure_ascii=False)
        )
        return analysis, {"action": "fallback_runbook_recovery", "host": host}

    models = [GEMINI_MODEL]
    models.extend(model for model in GEMINI_FALLBACK_MODELS if model not in models)
    last_error = None

    for model in models:
        for attempt in range(1, max(GEMINI_MAX_ATTEMPTS, 1) + 1):
            try:
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=f"Phân tích sự cố: {incident_details}",
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        tools=AGENT_TOOLS,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            maximum_remote_calls=max(GEMINI_MAX_REMOTE_CALLS, 0)
                        )
                    )
                )
                full_text = response.text or ""
                return full_text if full_text else "AI không phản hồi.", parse_proposal(full_text)
            except Exception as e:  # noqa: BLE001 - SDK exposes heterogeneous transport errors
                last_error = e
                retryable = is_retryable_gemini_error(e)
                logger.warning(
                    "Gemini call failed: model=%s attempt=%s/%s retryable=%s error=%s",
                    model,
                    attempt,
                    GEMINI_MAX_ATTEMPTS,
                    retryable,
                    e,
                )
                if not retryable or attempt >= GEMINI_MAX_ATTEMPTS:
                    break
                await asyncio.sleep(GEMINI_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

    logger.error("Gemini unavailable after retries/fallback models: %s", last_error)
    return fallback_analysis(last_error)


async def process_single_alert(alert: dict) -> None:
    """
    Xử lý logic cho một alert đơn lẻ (async).

    NEW WORKFLOW (Phase 7):
    - AI phân tích sự cố
    - Gửi hướng dẫn xử lí chi tiết cho Admin (không có buttons)
    - Lưu context vào Redis để verify sau
    - Kích hoạt task verification sau 5-10 phút
    """
    ACTIVE_TASKS.inc()
    start_time = time.time()
    try:
        alert_name  = alert["labels"].get("alertname", "Unknown")
        instance    = alert["labels"].get("instance", "Unknown")
        if alert.get("status") == "resolved":
            _clear_alert_cooldown(alert)
            resolved_incident_id = _mark_matching_incident_recovery_signal(alert)
            if not _reserve_alert_notification(alert, "resolved"):
                logger.info(
                    "Skipping duplicate resolved notification: alert=%s instance=%s",
                    alert_name,
                    instance,
                )
                ALERTS_PROCESSED_TOTAL.labels(status='deduped').inc()
                return
            incident_suffix = f" (ID: `{resolved_incident_id}`)" if resolved_incident_id else ""
            send_telegram_message(
                f"🔎 *ALERT ĐÃ HẾT, ĐANG XÁC MINH:* {alert_name} "
                f"trên `{instance}`{incident_suffix}"
            )
            ALERTS_PROCESSED_TOTAL.labels(status='verification_pending').inc()
            return

        if not _reserve_alert_processing(alert):
            logger.info(
                "Skipping duplicate alert within cooldown: alert=%s instance=%s cooldown=%ss",
                alert_name,
                instance,
                ALERT_AI_COOLDOWN_SECONDS,
            )
            ALERTS_PROCESSED_TOTAL.labels(status='deduped').inc()
            return

        if not _reserve_alert_notification(alert, "firing"):
            logger.info(
                "Skipping duplicate firing notification: alert=%s instance=%s",
                alert_name,
                instance,
            )
            ALERTS_PROCESSED_TOTAL.labels(status='deduped').inc()
            return

        try:
            run_shadow_if_enabled(alert)
        except Exception:
            logger.exception("Unified-core shadow failed; continuing legacy pipeline")

        incident_details = build_incident_details(alert)
        rule_analysis, rule_proposal = deterministic_diagnosis(alert)
        rag_context = ""

        if rule_analysis:
            ai_analysis = rule_analysis
            rag = get_rag_instance()
            rag_context = rag.query_knowledge(incident_details, alert_name=alert_name) if rag else ""
            if rag_context:
                logger.info("Retrieved RAG context for deterministic diagnosis: %s", alert_name)
            proposal = rule_proposal
        else:
            ai_analysis, proposal = await run_agent_workflow(incident_details, alert_name=alert_name)
        duration = time.time() - start_time
        AI_WORKFLOW_LATENCY_SECONDS.observe(duration)
        ALERTS_PROCESSED_TOTAL.labels(status='success').inc()

        incident_id = uuid.uuid4().hex[:8]
        incident_context = {
            "alert_name": alert_name,
            "instance": instance,
            "labels": alert.get("labels", {}),
            "annotations": alert.get("annotations", {}),
            "incident_details": incident_details,
            "ai_analysis": ai_analysis,
            "rag_context": rag_context,
            "proposal": proposal,
            "alert_identity": _alert_identity(alert),
            "starts_at": str(alert.get("startsAt") or ""),
            "fingerprint": alert.get("fingerprint"),
            "status": "firing",
            "timestamp": datetime.now(VN_TZ).isoformat(),
        }
        save_incident_to_redis(incident_id, incident_context)
        _link_active_incident(alert, incident_id)

        report = _format_alert_report(alert, incident_id, proposal, ai_analysis)

        # Gửi hướng dẫn cho admin (NO buttons)
        send_telegram_message(report, parse_mode=None)

        # PHASE 9: Schedule verification task sau 5-10 phút (300-600 seconds)
        verification_countdown = 300  # 5 minutes (có thể điều chỉnh)
        verify_resolution_task.apply_async(
            args=[incident_id, alert_name, instance],
            countdown=verification_countdown
        )
        logger.info(f"📋 Scheduled verification for incident {incident_id} in {verification_countdown}s")

    except Exception as e:
        ALERTS_PROCESSED_TOTAL.labels(status='failure').inc()
        logger.error(f"Error processing alert: {e}")
        raise  # re-raise để Celery task biết alert này thất bại
    finally:
        ACTIVE_TASKS.dec()


# PHASE 9: Automatic Verification Task
# ─────────────────────────────────────────────────────────────────────
async def verify_resolution(incident_id: str, alert_name: str, instance: str):
    """
    PHASE 9: Automatic Verification - kiểm lại sau 5-10 phút

    1. Query Prometheus để lấy metrics hiện tại
    2. So sánh với alert threshold
    3. Gửi báo cáo về kết quả (resolved/failed)
    4. Lưu vào ChromaDB với outcome
    """
    logger.info(f"🔍 Starting verification for incident {incident_id} ({alert_name} on {instance})")

    try:
        # Retrieve incident context từ Redis
        try:
            ctx_raw = redis_client.get(f"incident:{incident_id}")
        except redis.RedisError as e:
            logger.error(f"Redis read error during verification: {e}")
            send_telegram_message("⚠️ Không thể xác nhận kết quả vì Redis unavailable")
            return

        if not ctx_raw:
            logger.warning(f"Incident context expired for {incident_id}")
            send_telegram_message(f"⚠️ Context hết hạn cho sự cố `{incident_id}`")
            return

        ctx = json.loads(ctx_raw)

        # Query Prometheus metrics để kiểm lại
        # Cách 1: Gọi các diagnostic tools tương tự như AI analysis
        # Cách 2: Query trực tiếp Prometheus API (nếu cấu hình public)
        logger.info(f"📊 Checking current metrics for {instance}...")

        # Simulate health check (thực tế sẽ call Prometheus API hoặc diagnostic tools)
        is_resolved = await check_alert_resolved(alert_name, instance, ctx)

        if is_resolved:
            # Issue RESOLVED ✅
            outcome = "resolved_by_human"
            message = (
                f"✅ *SỰ CỐ ĐÃ ĐƯỢC KHÔI PHỤC*\n"
                f"Alert: {alert_name}\n"
                f"Server: {instance}\n"
                f"ID: {incident_id}\n\n"
                f"Metrics hiện tại đã trở lại bình thường."
            )
        else:
            # Issue STILL FAILING ❌
            outcome = "failed_to_resolve"
            message = (
                f"❌ *SỰ CỐ VẪN TỒN TẠI*\n"
                f"Alert: {alert_name}\n"
                f"Server: {instance}\n"
                f"ID: {incident_id}\n\n"
                f"⚠️ Các metrics vẫn còn cao.\n"
                f"💡 Gợi ý: Hãy thử giải pháp thay thế hoặc escalate."
            )

        # Send verification report
        send_telegram_message(message)

        # Save to ChromaDB with outcome
        rag = get_rag_instance()
        if rag:
            rag.save_incident(
                alert_name=ctx["alert_name"],
                description=ctx["incident_details"],
                ai_analysis=ctx["ai_analysis"],
                resolution=(ctx.get("proposal") or {}).get("action", "manual_fix"),
                outcome=outcome
            )
            logger.info(f"✅ Saved incident to ChromaDB with outcome: {outcome}")

        # Keep failed incidents available for admin feedback until their Redis TTL expires.
        if is_resolved:
            try:
                redis_client.delete(f"incident:{incident_id}")
            except redis.RedisError as e:
                logger.warning(f"Error deleting resolved incident from Redis: {e}")

    except Exception as e:  # noqa: BLE001 - fail closed when a checker dependency errors
        logger.error(f"Error during verification: {e}")
        send_telegram_message(f"⚠️ Lỗi kiểm tra kết quả: {e!s}")


async def check_alert_resolved(alert_name: str, instance: str, incident_context: dict | None = None) -> bool:
    """
    Check nếu alert đã được resolve bằng cách query Prometheus.

    Thực hiện:
    1. Query Prometheus API để lấy current metrics
    2. Compare với alert threshold
    3. Return True nếu metrics < threshold

    Example:
    - alert_name: "node_cpu_high"
    - instance: "10.10.1.68:9100"
    - Return: True nếu CPU < 80%
    """
    try:
        logger.info(f"Checking if {alert_name} is resolved on {instance}...")

        checker = get_prometheus_checker()
        labels = _labels_from_incident_context(incident_context or {})
        is_resolved = checker.is_alert_resolved(alert_name, instance, labels)

        if is_resolved:
            logger.info(f"✅ Alert {alert_name} is RESOLVED")
        else:
            logger.warning(f"❌ Alert {alert_name} is STILL FAILING")

        # Log metrics for debugging
        metrics = checker.get_alert_metrics(instance)
        logger.info(f"📊 Current metrics: {metrics}")

        return is_resolved

    except Exception as e:  # noqa: BLE001 - Celery retry boundary
        logger.error(f"Error checking alert resolution: {e}")
        # Default to failed if can't determine
        return False


@celery_app.task(name="verify_resolution_task", bind=True, max_retries=2)
def verify_resolution_task(self, incident_id: str, alert_name: str, instance: str):
    """
    Celery task để verify resolution (chạy sau 5-10 phút)

    Parameters:
    - incident_id: ID của incident
    - alert_name: Tên alert (e.g., node_cpu_high)
    - instance: Instance bị ảnh hưởng (e.g., 10.10.1.68)
    """
    try:
        logger.info(f"🔄 Running verification for {incident_id}")
        asyncio.run(verify_resolution(incident_id, alert_name, instance))
    except Exception as e:  # noqa: BLE001 - Celery retry boundary
        logger.error(f"Verification task failed: {e}")
        # Retry 1 lần sau 1 phút
        raise self.retry(exc=e, countdown=60)
# - FIX #2: Chạy TẤT CẢ alerts trong một lần asyncio.run() duy nhất với gather()
#   thay vì gọi asyncio.run() lặp lại trong vòng for → tốn tài nguyên tạo/hủy event loop
# - FIX #1: Tách xử lý lỗi per-alert ra khỏi retry của toàn bộ task.
#   Logic cũ: 1 alert lỗi → retry TOÀN BỘ task → các alert đã thành công bị xử lý lại.
#   Logic mới: gather(return_exceptions=True) thu thập lỗi từng alert riêng biệt;
#   chỉ retry task nếu có lỗi hệ thống thực sự (ví dụ Redis/network down).
@celery_app.task(name="process_admin_feedback_task", bind=True, max_retries=2)
def process_admin_feedback_task(self, incident_id: str, admin_feedback: str, chat_id: str | None = None):
    """Celery task xử lý góp ý của admin gửi qua Telegram."""
    try:
        asyncio.run(process_admin_feedback(incident_id, admin_feedback, chat_id))
    except Exception as e:  # noqa: BLE001 - Celery retry boundary
        logger.error("Admin feedback task failed: %s", e)
        raise self.retry(exc=e, countdown=30)


@celery_app.task(name="review_tool_change_task", bind=True, max_retries=2)
def review_tool_change_task(self, tool_name: str, revision_id: str):
    """Create a pending runbook draft for a registered tool revision."""
    try:
        draft = create_runbook_draft(tool_name=tool_name, revision_id=revision_id)
        notification_sent = notify_runbook_draft_for_approval(draft)
        logger.info("Created runbook draft %s for tool %s revision %s", draft["draft_id"], tool_name, revision_id)
        return {"status": "draft_created", "draft_id": draft["draft_id"], "notification_sent": notification_sent}
    except Exception as e:  # noqa: BLE001 - Celery retry boundary
        logger.error("Tool change review failed: tool=%s revision=%s error=%s", tool_name, revision_id, e)
        raise self.retry(exc=e, countdown=30)


@celery_app.task(name="process_alerts_task", bind=True, max_retries=3)
def process_alerts_task(self, payload_dict: dict):
    """Celery task xử lý alert payload từ Prometheus."""
    alerts = payload_dict.get("alerts", [])
    if not alerts:
        logger.info("No alerts in payload, skipping.")
        return

    async def _run_all():
        semaphore = asyncio.Semaphore(ALERT_BATCH_CONCURRENCY)

        async def _run_bounded(alert: dict):
            async with semaphore:
                return await process_single_alert(alert)

        # gather với return_exceptions=True để không dừng lại khi 1 alert lỗi
        results = await asyncio.gather(
            *[_run_bounded(alert) for alert in alerts],
            return_exceptions=True
        )
        # Ghi log các alert bị lỗi mà không làm ảnh hưởng đến alert khác
        failed = [
            (i, str(exc)) for i, exc in enumerate(results)
            if isinstance(exc, Exception)
        ]
        if failed:
            for idx, err in failed:
                alert_name = alerts[idx].get("labels", {}).get("alertname", "unknown")
                logger.error(f"Alert[{idx}] '{alert_name}' failed: {err}")
            # Chỉ raise nếu TẤT CẢ alerts đều thất bại (lỗi hệ thống)
            if len(failed) == len(alerts):
                raise RuntimeError(f"All {len(alerts)} alerts failed. Last error: {failed[-1][1]}")

    try:
        asyncio.run(_run_all())
    except RuntimeError as e:
        # Retry khi toàn bộ batch thất bại (thường do lỗi kết nối hệ thống)
        raise self.retry(exc=e, countdown=10)
