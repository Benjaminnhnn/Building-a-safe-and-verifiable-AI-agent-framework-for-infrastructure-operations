# Kết quả chẩn đoán

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class RootCauseHypothesis(BaseModel):
    # PLACEHOLDER LÀ GÌ:
    # hypothesis_id là ID của một giả thuyết root cause.
    # LẤY Ở ĐÂU:
    # Nên được Diagnosis Agent sinh khi phân tích incident.
    # SỬA NHƯ NÀO:
    # Khi test có thể dùng "hyp-db-stopped", nhưng khi chạy thật nên sinh theo diagnosis/run.
    hypothesis_id: str
    # Stable operational rule identifier. It is produced from observed evidence,
    # never copied from an evaluation scenario id.
    cause_code: str | None = None
    # PLACEHOLDER: Root cause thật mà agent kết luận, ví dụ "PostgreSQL container stopped".
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)

    # PLACEHOLDER: Danh sách resource_id thật bị ảnh hưởng bởi giả thuyết này.
    affected_resources: list[str]
    # PLACEHOLDER: Danh sách evidence_id thật ủng hộ giả thuyết.
    supporting_evidence_refs: list[str] = Field(min_length=1)
    # PLACEHOLDER: Danh sách evidence_id thật phản bác hoặc làm yếu giả thuyết.
    contradicting_evidence_refs: list[str] = Field(default_factory=list)

    # PLACEHOLDER: Tóm tắt lập luận thật dựa trên evidence.
    reasoning_summary: str


class DiagnosisResult(BaseModel):
    # PLACEHOLDER: ID diagnosis thật, nên sinh theo incident/run.
    diagnosis_id: str
    # PLACEHOLDER: ID incident thật đang được chẩn đoán.
    incident_id: str

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    top_hypothesis: RootCauseHypothesis | None = None
    alternative_hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)

    # PLACEHOLDER: Danh sách evidence_id thật mà diagnosis đã sử dụng.
    evidence_refs: list[str] = Field(default_factory=list)
    # PLACEHOLDER: Bằng chứng còn thiếu thật, ví dụ "docker ps chưa thu thập".
    missing_evidence: list[str] = Field(default_factory=list)

    confidence: float = Field(ge=0.0, le=1.0)
    ready_for_planning: bool

    # PLACEHOLDER: Ghi chú thật cho Planner/operator.
    notes: str | None = None
