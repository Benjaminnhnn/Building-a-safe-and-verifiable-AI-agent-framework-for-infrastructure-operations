# Ground Truth cho fault scenario

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# This finite map is for evaluation-fixture resource attribution only. It maps
# emitted Moodle alert/probe names to inventory resources; oracle labels are not
# used to infer incident impact or authorize an action.
_OBSERVED_SIGNAL_RESOURCES = {
    "moodle_endpoint_down": "moodle-app",
    "container_state_exited": "moodle-app",
    "MoodleWebContainerMissing": "moodle-app",
    "one ALB target becomes unhealthy": "moodle-app",
    "public service remains available through node A": "moodle-app",
    "MoodleNodeRouterFallbackFailed": "moodle-app",
    "MoodleApacheRouterMissing": "moodle-app",
    "MoodleWebContainerRestarting": "moodle-app",
    "MoodleSyntheticTransactionFailed": "moodle-app",
    "database connection failure in the synthetic transaction": "moodle-app",
    "MoodleNodeRdsTcpFailed": "moodle-app",
    "database hostname connects to the wrong destination": "moodle-app",
    "MoodleNodeRdsTcpLatencyHigh": "moodle-app",
    "MoodleNodeCpuHigh": "moodle-app",
    "elevated node CPU metric": "moodle-app",
    "increased transaction latency may occur": "moodle-app",
    "MoodleNodeMemoryPressure": "moodle-app",
    "MoodleScratchEnospc": "moodle-app",
    "MoodleRdsIngressDrift": "moodle-rds-security-group",
    "EFS fixture write returns an error": "moodledata-volume",
    "normal Moodle health endpoint may remain green": "moodle-app",
    "MoodleTrustedProxyConfigDrift": "moodle-app",
}

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
    category: str | None = None
    fault_group: str | None = None

    # PLACEHOLDER: Trạng thái ban đầu thật trước khi inject fault.
    initial_state: dict
    # PLACEHOLDER: Cách kích hoạt lỗi thật, ví dụ stop container, block port, fill disk.
    fault_trigger: dict
    # PLACEHOLDER: Tín hiệu quan sát thật từ Prometheus, Alertmanager, log, blackbox probe.
    observed_signals: list[str]

    @field_validator("observed_signals")
    @classmethod
    def validate_observed_signals(cls, signals: list[str]) -> list[str]:
        if any(not signal.strip() for signal in signals):
            raise ValueError("observed_signals must not contain blank values")
        if len(signals) != len(set(signals)):
            raise ValueError("observed_signals must be unique")
        return signals

    @field_validator("affected_resources")
    @classmethod
    def validate_affected_resources(cls, resources: list[str]) -> list[str]:
        if not resources or any(not resource.strip() for resource in resources):
            raise ValueError("affected_resources must contain non-blank resource IDs")
        if len(resources) != len(set(resources)):
            raise ValueError("affected_resources must be unique")
        return resources

    # PLACEHOLDER: Root cause đúng theo ground truth.
    # Dùng dict để tương thích validator hiện tại: category, service, component, cause, layer.
    expected_root_cause: dict[str, Any]
    description: str | None = None
    expected_impact: dict[str, Any] | None = None
    causal_order: list[str] = Field(default_factory=list)
    # PLACEHOLDER: Danh sách resource_id thật bị ảnh hưởng.
    affected_resources: list[str] = Field(default_factory=list)

    # PLACEHOLDER: Danh sách action_type hoặc action name thật được phép trong scenario.
    allowed_actions: list[str] = Field(default_factory=list)
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

    @model_validator(mode="before")
    @classmethod
    def adapt_current_moodle_contract(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        expected_root_cause = data.get("expected_root_cause")
        if not data.get("category") and isinstance(expected_root_cause, dict):
            data["category"] = expected_root_cause.get("category")
        if not data.get("allowed_actions"):
            remediation = data.get("allowed_remediation", [])
            if isinstance(remediation, list):
                data["allowed_actions"] = list(dict.fromkeys(
                    item["action"] for item in remediation
                    if isinstance(item, dict) and isinstance(item.get("action"), str)
                ))
        if not data.get("affected_resources"):
            signals = data.get("observed_signals", [])
            resources = list(dict.fromkeys(
                _OBSERVED_SIGNAL_RESOURCES[signal]
                for signal in signals
                if isinstance(signal, str) and signal in _OBSERVED_SIGNAL_RESOURCES
            )) if isinstance(signals, list) else []
            if resources:
                data["affected_resources"] = resources
        return data

    def get_communication_contract(self) -> CommunicationContract:
        return CommunicationContract(**self.communication_contract)
