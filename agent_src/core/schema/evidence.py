# Bằng chứng agent thu thập

from datetime import datetime, timezone
import json
import re
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from core.sanitization import contains_unredacted_secret
from .common import EvidenceType


class _FrozenMetadata(dict[str, str]):
    """Small immutable mapping; Evidence metadata values are scalar strings."""

    def _immutable(self, *_: object, **__: object) -> None:
        raise TypeError("Evidence metadata is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

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

    evidence_type: EvidenceType = EvidenceType.PROBE

    # PLACEHOLDER: Nguồn thu thập thật. Ví dụ "prometheus", "blackbox", "docker", "log".
    source: str = Field(..., examples=["prometheus", "blackbox", "docker", "log"])
    observed_at: datetime | None = None
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # PLACEHOLDER: Tóm tắt bằng chứng thật, ví dụ "PostgreSQL exporter trả về up=0".
    summary: str
    # PLACEHOLDER: Đường dẫn hoặc tham chiếu tới bằng chứng gốc, ví dụ Prometheus query hoặc đường dẫn log.
    raw_ref: str | None = None
    # PLACEHOLDER: Hash thật của nội dung evidence đã lưu, dùng để chứng minh evidence không bị sửa.
    content_hash: str

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    redacted: StrictBool = True
    # PLACEHOLDER: Metadata thật theo từng nguồn, ví dụ query Prometheus, container_name, log_file.
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_content_hash(self):
        if not self.content_hash.startswith(
            "pending"
        ) and not self.content_hash.startswith("sha256:"):
            raise ValueError("content_hash must start with 'sha256:' or 'pending'")
        if not self.redacted:
            raise ValueError("Evidence must be redacted before storage or model use")
        values = [
            self.evidence_id,
            self.incident_id,
            self.resource_id,
            self.source,
            self.summary,
            self.raw_ref or "",
            json.dumps(self.metadata, ensure_ascii=False),
        ]
        if any(contains_unredacted_secret(value) for value in values):
            raise ValueError("Evidence contains an unredacted secret")
        if self.collected_at.tzinfo is None or self.collected_at.utcoffset() is None:
            raise ValueError("Evidence collected_at must be timezone-aware")
        if self.observed_at is None:
            object.__setattr__(self, "observed_at", self.collected_at)
        elif self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Evidence observed_at must be timezone-aware")
        object.__setattr__(self, "metadata", _FrozenMetadata(self.metadata))
        return self
