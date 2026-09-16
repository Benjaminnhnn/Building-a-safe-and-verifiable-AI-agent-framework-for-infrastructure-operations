# Kết quả Independent Verifier

from datetime import datetime, timezone
from pydantic import BaseModel, Field


class ProbeResult(BaseModel):
    # PLACEHOLDER LÀ GÌ:
    # name là tên probe thật mà Verifier chạy.
    # LẤY Ở ĐÂU:
    # Lấy từ communication contract của scenario hoặc danh sách probe của Infrastructure Engineer.
    # SỬA NHƯ NÀO:
    # Ví dụ dùng "moodle_login", "db_connection", "forbidden_db_public_access".
    name: str
    passed: bool
    # PLACEHOLDER: Chi tiết kết quả probe thật, ví dụ status code, latency, lỗi kết nối.
    details: str


class VerificationResult(BaseModel):
    # PLACEHOLDER: ID verification thật, nên sinh theo incident/run.
    verification_id: str
    # PLACEHOLDER: ID incident thật đang được verify.
    incident_id: str

    health_passed: bool
    communication_contract_passed: bool
    stability_seconds: int

    allowed_probes: list[ProbeResult] = Field(default_factory=list)
    forbidden_probes: list[ProbeResult] = Field(default_factory=list)
    related_probes: list[ProbeResult] = Field(default_factory=list)

    # PLACEHOLDER: Verdict thật của Verifier sau khi kiểm tra health, communication contract và stability.
    verdict: str = Field(..., examples=["resolved", "not_resolved"])
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
