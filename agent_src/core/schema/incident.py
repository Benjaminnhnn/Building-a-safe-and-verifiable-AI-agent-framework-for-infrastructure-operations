# Sự cố đang xử lý

from datetime import datetime, timezone
from pydantic import BaseModel, Field

from .common import IncidentStatus


class Incident(BaseModel):
    # PLACEHOLDER LÀ GÌ:
    # incident_id là ID của một sự cố thật.
    # LẤY Ở ĐÂU:
    # Nên sinh từ alert batch/run ID hoặc từ Incident Core.
    # SỬA NHƯ NÀO:
    # Khi test thủ công có thể đặt như "inc-db-01-001", nhưng khi chạy thật nên để hệ thống sinh.
    incident_id: str
    # PLACEHOLDER: Fingerprint thật dùng để gộp alert trùng. Lấy từ Alertmanager labels hoặc hash labels chính.
    fingerprint: str
    # PLACEHOLDER: Tiêu đề sự cố thật, ví dụ "Moodle cannot connect to PostgreSQL".
    title: str
    status: IncidentStatus = IncidentStatus.OPEN
    # PLACEHOLDER: Severity thật từ Alertmanager hoặc policy, ví dụ "critical" hoặc "warning".
    severity: str = Field(..., examples=["critical", "warning"])

    # PLACEHOLDER: Danh sách resource_id thật bị ảnh hưởng.
    affected_resources: list[str]
    # PLACEHOLDER: Danh sách evidence_id thật gắn với incident này.
    evidence_refs: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # PLACEHOLDER: Giả thuyết hiện tại thật của Diagnosis Agent. Không dùng để kết luận RESOLVED.
    current_hypothesis: str | None = None
    resolved_by_verifier: bool = False
