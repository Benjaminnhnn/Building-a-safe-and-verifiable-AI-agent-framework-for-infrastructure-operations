# Hành động typed action

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from .common import ActionStatus, ActionType, Environment


class RollbackPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: StrictBool
    rollback_action_id: StrictStr | None = None
    # PLACEHOLDER: Cách rollback thật cho action này, ví dụ "stop container vừa start nếu related probes fail".
    method: str | None = None
    # PLACEHOLDER: Thời gian rollback dự kiến thật, tính bằng giây.
    expected_duration_seconds: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _require_rollback_method_when_available(self) -> RollbackPlan:
        if self.available and (self.method is None or not self.method.strip()):
            raise ValueError("an available rollback plan must define a nonblank method")
        return self


class TypedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # PLACEHOLDER LÀ GÌ:
    # action_id là ID của một hành động agent đề xuất hoặc thực thi.
    # LẤY Ở ĐÂU:
    # Nên được Planner/Orchestrator sinh theo incident/run.
    # SỬA NHƯ NÀO:
    # Khi test có thể dùng "act-db-01-start-container", khi chạy thật nên sinh tự động và ghi vào audit.
    action_id: str
    status: ActionStatus = ActionStatus.PROPOSED
    idempotency_key: str | None = None
    # PLACEHOLDER: ID incident thật mà action này xử lý.
    incident_id: str

    action_type: ActionType
    # PLACEHOLDER: resource_id thật của mục tiêu action. Phải tồn tại trong Resource model.
    target_resource_id: str
    environment: Environment

    # PLACEHOLDER: Lý do thật, bắt buộc dựa trên evidence chứ không viết chung chung.
    reason: str
    # PLACEHOLDER: Danh sách evidence_id thật làm căn cứ cho action. Không có evidence thì action bị reject.
    evidence_refs: list[str] = Field(min_length=1)

    # PLACEHOLDER: Tham số thật cho adapter, ví dụ container_name, compose_service, playbook.
    parameters: dict[str, str] = Field(default_factory=dict)
    # PLACEHOLDER: Điều kiện thật cần kiểm tra trước khi chạy action.
    preconditions: list[str] = Field(default_factory=list)
    # PLACEHOLDER: Kết quả mong đợi thật sau khi action chạy.
    expected_outcome: str

    reversible: StrictBool
    rollback_plan: RollbackPlan

    requires_approval: StrictBool = False

    @property
    def action_hash(self) -> str:
        """Hash all action contract fields except mutable execution status."""
        payload = self.model_dump(mode="json", exclude={"status"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
