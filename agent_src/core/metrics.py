# Prometheus Metrics cho AI Ops Agent.

from fastapi import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Metrics định nghĩa
ALERTS_PROCESSED_TOTAL = Counter(
    "aiops_alerts_processed_total",
    "Tổng số alerts đã được xử lý bởi AI Agent",
    ["status"],  # success, failure, resolved, deduped
)

WEBHOOK_EVENTS_TOTAL = Counter(
    "aiops_webhook_events_total",
    "Tong so alerts nhan tai webhook theo ket qua enqueue",
    ["status"],  # enqueued, deduped, rejected, error
)

UNIFIED_SHADOW_EVENTS_TOTAL = Counter(
    "aiops_unified_shadow_events_total",
    "Tong so alert duoc unified control plane xu ly o shadow mode",
    ["status"],  # observed, skipped, error
)

AI_WORKFLOW_LATENCY_SECONDS = Histogram(
    "aiops_ai_workflow_duration_seconds",
    "Thời gian hoàn tất workflow AI (RAG + LLM)",
    buckets=(1, 5, 10, 30, 60, 120, 300),
)

# Để đo số task đang xử lý đồng thời
ACTIVE_TASKS = Gauge(
    "aiops_active_tasks", "Số lượng alert đang được phân tích đồng thời"
)

CELERY_QUEUE_DEPTH = Gauge(
    "aiops_celery_queue_depth", "So luong Celery tasks dang cho trong Redis broker"
)


def get_metrics_response():
    """Trả về dữ liệu metrics theo chuẩn Prometheus."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
