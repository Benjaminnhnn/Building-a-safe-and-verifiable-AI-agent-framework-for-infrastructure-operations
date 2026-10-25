"""Deterministic validation of LLM diagnosis output against stored evidence.

An LLM hypothesis is accepted only when every citation resolves to a stored,
fresh Prometheus sample whose query, instance and value match what the model
claimed, and when the cited samples satisfy the signal predicates of a cause in
the closed vocabulary. Anything else is rejected with an explicit violation
code; the caller fails closed to ``insufficient_evidence``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.reasoning.cause_vocabulary import CAUSE_VOCABULARY

MAX_SAMPLE_AGE_SECONDS = 120
MAX_FUTURE_SKEW_SECONDS = 30
MAX_CITATION_WINDOW_SECONDS = 60
MAX_HYPOTHESES = 3
MAX_CITATIONS = 12
MAX_RUNBOOK_REFS = 5
_VALUE_TOLERANCE = 1e-6

# Diagnosis output must never carry an action; planning stays in the catalog.
_FORBIDDEN_KEYS = {
    "action", "actions", "action_id", "catalog_action_id", "command", "commands",
    "remediation", "remediation_plan", "execute", "execution", "script", "shell",
    "playbook", "rollback",
}

OUTPUT_NOT_JSON = "output_not_json"
SCHEMA_INVALID = "schema_invalid"
ACTION_OUTSIDE_SCOPE = "action_outside_diagnosis_scope"
UNKNOWN_CAUSE_CODE = "unknown_cause_code"
CONFIDENCE_OUT_OF_RANGE = "confidence_out_of_range"
NO_CITATIONS = "no_citations"
CITATION_NOT_FOUND = "citation_not_found"
CITATION_SOURCE_NOT_ALLOWED = "citation_source_not_allowed"
CITATION_QUERY_MISMATCH = "citation_query_mismatch"
CITATION_INSTANCE_MISMATCH = "citation_instance_mismatch"
CITATION_VALUE_MISMATCH = "citation_value_mismatch"
CITATION_STALE = "citation_stale"
CITATION_WINDOW_TOO_WIDE = "citation_window_too_wide"
REQUIRED_SIGNAL_NOT_CITED = "required_signal_not_cited"
SIGNAL_PREDICATE_NOT_SATISFIED = "signal_predicate_not_satisfied"
RUNBOOK_REF_NOT_FOUND = "runbook_ref_not_found"
CITATION_CONFLICTS_WITH_STORED = "citation_conflicts_with_stored_sample"


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=200)
    query_id: str = Field(min_length=1, max_length=200)
    instance: str | None = Field(default=None, max_length=200)
    value: float


class LLMHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause_code: str = Field(min_length=1, max_length=120)
    confidence: float
    rationale: str = Field(default="", max_length=2000)
    citations: list[Citation] = Field(default_factory=list, max_length=MAX_CITATIONS)
    runbook_refs: list[str] = Field(default_factory=list, max_length=MAX_RUNBOOK_REFS)


class LLMDiagnosisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern="^(hypothesis|insufficient_evidence)$")
    hypotheses: list[LLMHypothesis] = Field(default_factory=list, max_length=MAX_HYPOTHESES)
    abstain_reason: str = Field(default="", max_length=1000)


@dataclass(frozen=True)
class StoredSample:
    evidence_id: str
    source: str
    query_id: str | None
    value: float | None
    instance: str | None
    collected_at: datetime | None


@dataclass
class ValidationResult:
    accepted: bool
    abstained: bool = False
    selected: LLMHypothesis | None = None
    selected_index: int | None = None
    parsed: LLMDiagnosisOutput | None = None
    violations: list[dict[str, Any]] = field(default_factory=list)

    def violation_codes(self) -> list[str]:
        return sorted({item["code"] for item in self.violations})


def index_evidence(evidence: list[Any]) -> dict[str, StoredSample]:
    """Index stored evidence by ID, decoding Prometheus sample payloads."""
    indexed: dict[str, StoredSample] = {}
    for item in evidence:
        evidence_id = getattr(item, "evidence_id", None)
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue
        source = str(getattr(item, "source", "") or "")
        query_id = value = instance = None
        metadata = getattr(item, "metadata", None)
        if source == "prometheus" and isinstance(metadata, dict):
            try:
                payload = json.loads(metadata.get("payload_json", "{}"))
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                raw_query = payload.get("query_id")
                raw_value = payload.get("sample_value")
                labels = payload.get("labels")
                query_id = raw_query if isinstance(raw_query, str) else None
                if (
                    isinstance(raw_value, (int, float))
                    and not isinstance(raw_value, bool)
                    and math.isfinite(raw_value)
                ):
                    value = float(raw_value)
                if isinstance(labels, dict) and labels.get("instance"):
                    instance = str(labels["instance"])
        collected_at = getattr(item, "collected_at", None)
        if not isinstance(collected_at, datetime) or collected_at.tzinfo is None:
            collected_at = None
        indexed[evidence_id] = StoredSample(evidence_id, source, query_id, value, instance, collected_at)
    return indexed


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1) if match else stripped


def _forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().lower() in _FORBIDDEN_KEYS:
                found.add(str(key))
            found |= _forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            found |= _forbidden_keys(child)
    return found


def parse_llm_output(text: str) -> tuple[LLMDiagnosisOutput | None, list[dict[str, Any]]]:
    """Parse raw model text; return output-level violations on failure."""
    try:
        raw = json.loads(_strip_fences(text or ""))
    except (TypeError, ValueError):
        return None, [{"hypothesis_index": None, "code": OUTPUT_NOT_JSON, "detail": "model output is not JSON"}]
    if not isinstance(raw, dict):
        return None, [{"hypothesis_index": None, "code": OUTPUT_NOT_JSON, "detail": "model output is not a JSON object"}]
    forbidden = _forbidden_keys(raw)
    if forbidden:
        return None, [{
            "hypothesis_index": None,
            "code": ACTION_OUTSIDE_SCOPE,
            "detail": f"diagnosis output contains action fields: {sorted(forbidden)}",
        }]
    try:
        return LLMDiagnosisOutput.model_validate(raw), []
    except ValidationError as exc:
        return None, [{
            "hypothesis_index": None,
            "code": SCHEMA_INVALID,
            "detail": f"{exc.error_count()} schema error(s)",
        }]


def validate_hypothesis(
    hypothesis: LLMHypothesis,
    samples: dict[str, StoredSample],
    *,
    now: datetime,
    index: int = 0,
    runbook_chunk_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return citation violations for one hypothesis; empty means accepted.

    ``runbook_chunk_ids`` is the set of chunk IDs shown to the model; ``None``
    skips the runbook check (used when re-validating a cached decision).
    """
    violations: list[dict[str, Any]] = []

    def flag(code: str, detail: str) -> None:
        violations.append({"hypothesis_index": index, "code": code, "detail": detail})

    definition = CAUSE_VOCABULARY.get(hypothesis.cause_code)
    if definition is None:
        flag(UNKNOWN_CAUSE_CODE, f"{hypothesis.cause_code!r} is not in the reviewed cause vocabulary")
    if not math.isfinite(hypothesis.confidence) or not 0.0 <= hypothesis.confidence <= 1.0:
        flag(CONFIDENCE_OUT_OF_RANGE, "confidence must be within [0, 1]")
    if runbook_chunk_ids is not None:
        for ref in hypothesis.runbook_refs:
            if ref not in runbook_chunk_ids:
                flag(RUNBOOK_REF_NOT_FOUND, f"{ref} was not among the runbook chunks provided")
    if not hypothesis.citations:
        flag(NO_CITATIONS, "a hypothesis must cite stored evidence")
        return violations

    cited_values: dict[str, list[float]] = {}
    cited_times: list[datetime] = []
    for citation in hypothesis.citations:
        stored = samples.get(citation.evidence_id)
        if stored is None:
            flag(CITATION_NOT_FOUND, f"{citation.evidence_id} is not in the stored evidence set")
            continue
        if stored.source != "prometheus" or stored.query_id is None or stored.value is None:
            flag(CITATION_SOURCE_NOT_ALLOWED, f"{citation.evidence_id} is not a decodable Prometheus sample")
            continue
        if stored.query_id != citation.query_id:
            flag(CITATION_QUERY_MISMATCH, f"{citation.evidence_id} stores {stored.query_id}, not {citation.query_id}")
            continue
        if (citation.instance or None) != stored.instance:
            flag(CITATION_INSTANCE_MISMATCH, f"{citation.evidence_id} instance does not match the stored sample")
            continue
        if abs(stored.value - citation.value) > _VALUE_TOLERANCE * max(1.0, abs(stored.value)):
            flag(CITATION_VALUE_MISMATCH, f"{citation.evidence_id} stores {stored.value:g}, model claimed {citation.value:g}")
            continue
        if stored.collected_at is None:
            flag(CITATION_STALE, f"{citation.evidence_id} has no timezone-aware collection time")
            continue
        age = (now - stored.collected_at.astimezone(timezone.utc)).total_seconds()
        if age > MAX_SAMPLE_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
            flag(CITATION_STALE, f"{citation.evidence_id} is {age:.0f}s old")
            continue
        # A model cannot pick one side of contradictory telemetry.
        conflicting = {
            sample.value for sample in samples.values()
            if sample.query_id == stored.query_id and sample.instance == stored.instance and sample.value is not None
        }
        if len(conflicting) > 1:
            flag(CITATION_CONFLICTS_WITH_STORED, f"{citation.evidence_id} conflicts with another stored sample for the same query and instance")
            continue
        cited_values.setdefault(stored.query_id, []).append(stored.value)
        cited_times.append(stored.collected_at.astimezone(timezone.utc))

    if cited_times and (max(cited_times) - min(cited_times)).total_seconds() > MAX_CITATION_WINDOW_SECONDS:
        flag(CITATION_WINDOW_TOO_WIDE, "cited samples do not belong to one 60-second window")

    if definition is not None:
        for requirement in definition.requirements:
            if requirement.query_id not in cited_values:
                flag(REQUIRED_SIGNAL_NOT_CITED, f"{hypothesis.cause_code} requires a valid citation of {requirement.query_id}")
                continue
            # ``all_eq`` is checked against every stored sample so a model
            # cannot cherry-pick the healthy node and omit the failing one.
            values = (
                [
                    sample.value for sample in samples.values()
                    if sample.query_id == requirement.query_id and sample.value is not None
                ]
                if requirement.predicate == "all_eq"
                else cited_values[requirement.query_id]
            )
            if not requirement.holds(values):
                flag(SIGNAL_PREDICATE_NOT_SATISFIED, f"stored samples do not satisfy {requirement.describe()}")
    return violations


def validate_llm_output(
    text: str,
    evidence: list[Any],
    *,
    now: datetime | None = None,
    runbook_chunk_ids: set[str] | None = None,
) -> ValidationResult:
    """Parse and validate model output; select the first fully cited hypothesis."""
    parsed, violations = parse_llm_output(text)
    if parsed is None:
        return ValidationResult(accepted=False, violations=violations)
    if parsed.status == "insufficient_evidence" or not parsed.hypotheses:
        return ValidationResult(accepted=False, abstained=True, parsed=parsed)
    samples = index_evidence(evidence)
    current = now or datetime.now(timezone.utc)
    all_violations: list[dict[str, Any]] = []
    for index, hypothesis in enumerate(parsed.hypotheses):
        found = validate_hypothesis(
            hypothesis, samples, now=current, index=index, runbook_chunk_ids=runbook_chunk_ids or set()
        )
        if not found:
            return ValidationResult(
                accepted=True,
                selected=hypothesis,
                selected_index=index,
                parsed=parsed,
                violations=all_violations,
            )
        all_violations.extend(found)
    return ValidationResult(accepted=False, parsed=parsed, violations=all_violations)
