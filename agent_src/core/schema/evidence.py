# Bằng chứng agent thu thập

from datetime import datetime, timezone
from pydantic import BaseModel, Field

class Evidence(BaseModel):
    # PLACEHOLDER LÀ GÌ:
    # evidence_id là ID của một bằng chứng cụ thể.
    # LẤY Ở ĐÂU:
    # Nên được hệ thống sinh tự động khi agent thu thập metric/log/probe.
    # SỬA NHƯ NÀO:
    # Khi tạo dữ liệu test hoặc ground truth, đặt ID có nghĩa như "ev-db-01-prometheus-up".
    evidence_id: str
    # PLACEHOLDER: ID sự cố thật mà evidence này thuộc về. Phải trùng với Incident.incident_id.
    incident_id: str
    # PLACEHOLDER: ID resource thật mà evidence nói tới. Phải trùng với Resource.resource_id.
    resource_id: str

    # PLACEHOLDER: Nguồn thu thập thật. Ví dụ "prometheus", "blackbox", "docker", "log".
    source: str = Field(..., examples=["prometheus", "blackbox", "docker", "log"])
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # PLACEHOLDER: Tóm tắt bằng chứng thật, ví dụ "PostgreSQL exporter trả về up=0".
    summary: str
    # PLACEHOLDER: Đường dẫn hoặc tham chiếu tới bằng chứng gốc, ví dụ Prometheus query hoặc đường dẫn log.
    raw_ref: str | None = None
    # PLACEHOLDER: Hash thật của nội dung evidence đã lưu, dùng để chứng minh evidence không bị sửa.
    content_hash: str

    redacted: bool = True
    # PLACEHOLDER: Metadata thật theo từng nguồn, ví dụ query Prometheus, container_name, log_file.
    metadata: dict[str, str] = Field(default_factory=dict)
    
