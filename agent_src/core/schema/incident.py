# Sự cố đang xử lý

from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import IncidentStatus


class Incident(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

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
    # Reference (verification id) of the Independent Verifier result that
    # justified RESOLVED. Only IncidentStateMachine.resolve_verified sets it.
    verifier_attestation_ref: str | None = None

    _STATE_MACHINE_ONLY: ClassVar[frozenset[str]] = frozenset(
        {"status", "resolved_by_verifier", "verifier_attestation_ref"}
    )

    def __init__(self, **data: Any) -> None:
        # An Incident is never *born* resolved or verifier-stamped: those fields
        # are applied afterwards by the state machine's audited transition.
        status = data.get("status", IncidentStatus.OPEN)
        if (
            status in {IncidentStatus.RESOLVED, IncidentStatus.RESOLVED.value}
            or data.get("resolved_by_verifier")
            or data.get("verifier_attestation_ref")
        ):
            raise ValueError(
                "an incident cannot be constructed as RESOLVED or verifier-stamped; "
                "only IncidentStateMachine.resolve_verified may resolve it"
            )
        super().__init__(**data)

    @model_validator(mode="after")
    def _resolved_requires_verifier_attestation(self) -> "Incident":
        resolved = self.status == IncidentStatus.RESOLVED
        if resolved and not (
            self.resolved_by_verifier
            and isinstance(self.verifier_attestation_ref, str)
            and self.verifier_attestation_ref.strip()
        ):
            raise ValueError("RESOLVED requires a verifier attestation reference")
        if not resolved and (self.resolved_by_verifier or self.verifier_attestation_ref):
            raise ValueError("verifier resolution markers require the RESOLVED status")
        return self

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._STATE_MACHINE_ONLY:
            raise ValueError(
                f"incident field {name!r} must be changed through IncidentStateMachine"
            )
        super().__setattr__(name, value)
