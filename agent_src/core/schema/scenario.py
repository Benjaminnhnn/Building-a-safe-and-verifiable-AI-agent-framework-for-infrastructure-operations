# Ground Truth cho fault scenario

from typing import Any

from pydantic import BaseModel, Field, model_validator

class CommunicationContract(BaseModel):
    allowed: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_overlap(self):
        overlap = set(self.allowed) & set(self.forbidden)
        if overlap:
            raise ValueError(f"overlap between allowed and forbidden: {overlap}")
        return self

class ScenarioGroundTruth(BaseModel):
    schema_version: str = "2.0"
    # PLACEHOLDER LÀ GÌ:
    # scenario_id là mã scenario trong bộ thí nghiệm.
    # LẤY Ở ĐÂU:
    # Lấy từ scenario matrix trong Planning.md, ví dụ DB-01, RES-01, NET-01.
    # SỬA NHƯ NÀO:
    # Khi viết file ground truth JSON, thay bằng mã scenario thật, không để tên ví dụ chung chung.
    scenario_id: str
    # PLACEHOLDER: Tên scenario thật, ví dụ "PostgreSQL stopped".
    scenario_name: str
    # PLACEHOLDER: Nhóm scenario thật, ví dụ database, resource, network, container, security.
    category: str
    fault_group: str | None = None

    # PLACEHOLDER: Trạng thái ban đầu thật trước khi inject fault.
    initial_state: dict
    # PLACEHOLDER: Cách kích hoạt lỗi thật, ví dụ stop container, block port, fill disk.
    fault_trigger: dict
    # PLACEHOLDER: Tín hiệu quan sát thật từ Prometheus, Alertmanager, log, blackbox probe.
    observed_signals: list[str]

    # PLACEHOLDER: Root cause đúng theo ground truth.
    # Dùng dict để tương thích validator hiện tại: category, service, component, cause, layer.
    expected_root_cause: dict[str, Any]
    description: str | None = None
    expected_impact: dict[str, Any] | None = None
    causal_order: list[str] = Field(default_factory=list)
    # PLACEHOLDER: Danh sách resource_id thật bị ảnh hưởng.
    affected_resources: list[str]

    # PLACEHOLDER: Danh sách action_type hoặc action name thật được phép trong scenario.
    allowed_actions: list[str]
    allowed_remediation: list[dict[str, Any]] = Field(default_factory=list)
    # PLACEHOLDER: Danh sách action nguy hiểm hoặc bị cấm trong scenario.
    forbidden_actions: list[str]

    # PLACEHOLDER: Tiêu chí phục hồi thật của scenario.
    recovery_criteria: list[str]
    # PLACEHOLDER: Communication contract thật cần kiểm tra allowed, forbidden, related.
    communication_contract: dict
    # PLACEHOLDER: Kế hoạch rollback/reset thật của scenario.
    rollback_plan: dict

    injector_script: str | None = None
    reset_script: str | None = None
    expected_detection_time_seconds: int | None = None
    expected_recovery_time_seconds: int | None = None
    difficulty_level: str = "medium"

    # PLACEHOLDER: Danh sách metric hoặc timestamp thật cần thu cho benchmark.
    metrics_required: list[str] = Field(default_factory=list)

    def get_communication_contract(self) -> CommunicationContract:
        return CommunicationContract(**self.communication_contract)
