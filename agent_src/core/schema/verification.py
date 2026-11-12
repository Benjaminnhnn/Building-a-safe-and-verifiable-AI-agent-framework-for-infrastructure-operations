# Kết quả Independent Verifier

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator, model_validator

from core.environment_profile import get_environment_profile

VerificationVerdict = Literal[
    "resolved", "not_resolved", "false_recovery", "collateral_damage", "not_stable"
]


class StabilityObservation(BaseModel):
    """Read-only health observation collected during a verifier window."""

    observed_at: datetime
    healthy: StrictBool
    simulated: StrictBool = True

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
    passed: StrictBool
    simulated: StrictBool = True
    # PLACEHOLDER: Chi tiết kết quả probe thật, ví dụ status code, latency, lỗi kết nối.
    details: str


class VerificationResult(BaseModel):
    # PLACEHOLDER: ID verification thật, nên sinh theo incident/run.
    verification_id: str
    # PLACEHOLDER: ID incident thật đang được verify.
    incident_id: str

    health_passed: StrictBool
    communication_contract_passed: StrictBool
    stability_seconds: StrictInt = Field(ge=0)
    stability_observations: list[StabilityObservation] = Field(default_factory=list)
    resolution_eligible: StrictBool = False

    allowed_probes: list[ProbeResult] = Field(default_factory=list)
    forbidden_probes: list[ProbeResult] = Field(default_factory=list)
    related_probes: list[ProbeResult] = Field(default_factory=list)

    # PLACEHOLDER: Verdict thật của Verifier sau khi kiểm tra health, communication contract và stability.
    verdict: VerificationVerdict = Field(..., examples=["resolved", "not_resolved"])
    simulated: StrictBool = True
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _resolved_verdict_needs_proof(self) -> "VerificationResult":
        """A "resolved" verdict is only representable with the evidence behind it."""
        if self.verdict != "resolved":
            if self.resolution_eligible:
                raise ValueError("only a resolved verdict can be resolution_eligible")
            return self
        if not (
            self.health_passed
            and self.communication_contract_passed
            and self.resolution_eligible
        ):
            raise ValueError(
                "resolved verdict requires health, communication contract and resolution eligibility"
            )
        if not self.simulated:
            required = get_environment_profile().verification.min_samples
            groups = (self.allowed_probes, self.forbidden_probes, self.related_probes)
            if (
                len(self.stability_observations) < required
                or any(item.simulated or not item.healthy for item in self.stability_observations)
                or any(probe.simulated for group in groups for probe in group)
            ):
                raise ValueError(
                    "a non-simulated resolved verdict requires enough healthy, non-simulated observations"
                )
        return self
