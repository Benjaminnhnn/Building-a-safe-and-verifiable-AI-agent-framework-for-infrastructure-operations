# Kết quả Independent Verifier

from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator


class StabilityObservation(BaseModel):
    """Read-only health observation collected during a verifier window."""

    observed_at: datetime
    healthy: bool

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stability observation timestamp must be timezone-aware")
        return value


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
    stability_observations: list[StabilityObservation] = Field(default_factory=list)
    resolution_eligible: bool = False

    allowed_probes: list[ProbeResult] = Field(default_factory=list)
    forbidden_probes: list[ProbeResult] = Field(default_factory=list)
    related_probes: list[ProbeResult] = Field(default_factory=list)

    # PLACEHOLDER: Verdict thật của Verifier sau khi kiểm tra health, communication contract và stability.
    verdict: str = Field(..., examples=["resolved", "not_resolved"])
    simulated: bool = False
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
