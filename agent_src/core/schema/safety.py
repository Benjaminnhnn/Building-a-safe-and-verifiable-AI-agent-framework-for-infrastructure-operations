# Quyết định Safety Gate

from datetime import datetime, timezone
from pydantic import BaseModel, Field

from .common import ActionDecision


class SafetyDecision(BaseModel):
    # PLACEHOLDER LÀ GÌ:
    # decision_id là ID của quyết định Safety Gate.
    # LẤY Ở ĐÂU:
    # Nên được Safety Engine sinh mỗi lần kiểm tra một action.
    # SỬA NHƯ NÀO:
    # Khi test có thể dùng "gate-act-001", khi chạy thật nên sinh tự động và ghi audit.
    decision_id: str
    # PLACEHOLDER: ID action thật đang được Safety Gate kiểm tra.
    action_id: str
    # PLACEHOLDER: ID incident thật liên quan đến action.
    incident_id: str

    decision: ActionDecision
    # PLACEHOLDER: Lý do thật của Safety Gate, ví dụ "action reversible", "target allowlisted".
    reasons: list[str] = Field(default_factory=list)

    # PLACEHOLDER: Danh sách evidence_id thật mà Safety Gate đã kiểm tra hoặc yêu cầu.
    required_evidence_refs: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    approval_required: bool = False
    # PLACEHOLDER: TTL thật cho approval nếu decision là REQUIRE_APPROVAL.
    approval_ttl_seconds: int | None = None
