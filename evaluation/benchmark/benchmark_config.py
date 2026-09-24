"""Benchmark harness: metrics recorder and run configuration.

Supports three methods: manual, ansible, ai_agent.
Records all timestamps and metrics required by PLANNING.md Section 8.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class BenchmarkMethod(str, Enum):
    """Identifies who or what performed the remediation."""

    MANUAL = "manual"
    ANSIBLE = "ansible"
    AI_AGENT = "ai_agent"


class RunTimestamps(BaseModel):
    """All timestamps for a single benchmark run.

    All fields are optional and, when set, must be timezone-aware.
    """

    model_config = ConfigDict(extra="forbid")

    t_inject: datetime | None = None
    t_detect: datetime | None = None
    t_incident: datetime | None = None
    t_plan: datetime | None = None
    t_gate: datetime | None = None
    t_execute_start: datetime | None = None
    t_execute_end: datetime | None = None
    t_verify: datetime | None = None
    t_resolved: datetime | None = None

    @field_validator(
        "t_inject",
        "t_detect",
        "t_incident",
        "t_plan",
        "t_gate",
        "t_execute_start",
        "t_execute_end",
        "t_verify",
        "t_resolved",
        mode="before",
    )
    @classmethod
    def _ensure_timezone_aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if v.tzinfo is None:
            raise ValueError("All timestamps must be timezone-aware (tzinfo must not be None).")
        return v

    @property
    def detection_time_seconds(self) -> float | None:
        """Elapsed seconds from fault injection to first detection."""
        if self.t_inject is None or self.t_detect is None:
            return None
        return (self.t_detect - self.t_inject).total_seconds()

    @property
    def remediation_time_seconds(self) -> float | None:
        """Elapsed seconds from execution start to verified resolution."""
        if self.t_execute_start is None or self.t_resolved is None:
            return None
        return (self.t_resolved - self.t_execute_start).total_seconds()


class RunResult(BaseModel):
    """Complete record of a single benchmark trial."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str
    method: BenchmarkMethod
    repetition: int
    timestamps: RunTimestamps

    # Correctness metrics
    rca_correct: bool | None = None
    recovery_success: bool | None = None
    false_recovery: bool | None = None  # health said ok but contract failed
    dangerous_action_blocked: bool | None = None
    forbidden_execution_count: int = 0
    rollback_success: bool | None = None

    # Efficiency metrics
    action_count: int = 0
    llm_call_count: int = 0

    # Verifier metrics
    audit_complete: bool | None = None
    verifier_resolved: bool | None = None  # only verifier set RESOLVED

    notes: str = ""


class BenchmarkRun(BaseModel):
    """Lightweight run descriptor used before results are available."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str
    method: BenchmarkMethod
    repetition: int
    ablation: Literal["none", "no_safety_gate", "no_verifier"] = "none"
    counterfactual_verdict: dict | None = None  # for no_verifier ablation
    shadow_would_execute: list[str] = []  # for no_safety_gate ablation


# ---------------------------------------------------------------------------
# Metrics recorder
# ---------------------------------------------------------------------------

class MetricsRecorder:
    """Append-only JSONL store for RunResult objects with summary computation."""

    _FILENAME = "benchmark_results.jsonl"

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._output_dir / self._FILENAME

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, result: RunResult) -> None:
        """Append *result* as a JSON line to the JSONL store."""
        line = result.model_dump_json() + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_all(self) -> list[RunResult]:
        """Return all recorded results in insertion order."""
        if not self._path.exists():
            return []
        results: list[RunResult] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    results.append(RunResult.model_validate_json(line))
        return results

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Compute aggregate metrics over all recorded runs."""
        results = self.load_all()
        total = len(results)
        if total == 0:
            return {
                "total_runs": 0,
                "passed": 0,
                "failed": 0,
                "rca_accuracy": None,
                "recovery_success_rate": None,
                "false_recovery_rate": None,
                "dangerous_action_block_rate": None,
                "forbidden_execution_total": 0,
                "avg_detection_time_seconds": None,
                "avg_remediation_time_seconds": None,
                "rollback_success_rate": None,
                "audit_completeness": None,
                "verifier_authority_rate": None,
            }

        # --- basic pass/fail (recovery_success) ---
        passed = sum(1 for r in results if r.recovery_success is True)
        failed = total - passed

        # --- RCA accuracy ---
        rca_with_verdict = [r for r in results if r.rca_correct is not None]
        rca_accuracy: float | None = None
        if rca_with_verdict:
            rca_accuracy = sum(1 for r in rca_with_verdict if r.rca_correct) / len(rca_with_verdict)

        # --- recovery success rate ---
        recovery_with_verdict = [r for r in results if r.recovery_success is not None]
        recovery_success_rate: float | None = None
        if recovery_with_verdict:
            recovery_success_rate = sum(1 for r in recovery_with_verdict if r.recovery_success) / len(recovery_with_verdict)

        # --- false recovery rate ---
        false_recovery_with_verdict = [r for r in results if r.false_recovery is not None]
        false_recovery_rate: float | None = None
        if false_recovery_with_verdict:
            false_recovery_rate = sum(1 for r in false_recovery_with_verdict if r.false_recovery) / len(false_recovery_with_verdict)

        # --- dangerous action block rate ---
        dangerous_with_verdict = [r for r in results if r.dangerous_action_blocked is not None]
        dangerous_action_block_rate: float | None = None
        if dangerous_with_verdict:
            dangerous_action_block_rate = sum(1 for r in dangerous_with_verdict if r.dangerous_action_blocked) / len(dangerous_with_verdict)

        # --- forbidden execution total ---
        forbidden_execution_total = sum(r.forbidden_execution_count for r in results)

        # --- avg detection time ---
        detection_times = [
            r.timestamps.detection_time_seconds
            for r in results
            if r.timestamps.detection_time_seconds is not None
        ]
        avg_detection_time_seconds: float | None = None
        if detection_times:
            avg_detection_time_seconds = sum(detection_times) / len(detection_times)

        # --- avg remediation time ---
        remediation_times = [
            r.timestamps.remediation_time_seconds
            for r in results
            if r.timestamps.remediation_time_seconds is not None
        ]
        avg_remediation_time_seconds: float | None = None
        if remediation_times:
            avg_remediation_time_seconds = sum(remediation_times) / len(remediation_times)

        # --- rollback success rate ---
        rollback_with_verdict = [r for r in results if r.rollback_success is not None]
        rollback_success_rate: float | None = None
        if rollback_with_verdict:
            rollback_success_rate = sum(1 for r in rollback_with_verdict if r.rollback_success) / len(rollback_with_verdict)

        # --- audit completeness ---
        audit_with_verdict = [r for r in results if r.audit_complete is not None]
        audit_completeness: float | None = None
        if audit_with_verdict:
            audit_completeness = sum(1 for r in audit_with_verdict if r.audit_complete) / len(audit_with_verdict)

        # --- verifier authority rate (only verifier resolved / total resolved) ---
        resolved_runs = [r for r in results if r.recovery_success is True]
        verifier_authority_rate: float | None = None
        if resolved_runs:
            verifier_resolved_count = sum(1 for r in resolved_runs if r.verifier_resolved is True)
            verifier_authority_rate = verifier_resolved_count / len(resolved_runs)

        return {
            "total_runs": total,
            "passed": passed,
            "failed": failed,
            "rca_accuracy": rca_accuracy,
            "recovery_success_rate": recovery_success_rate,
            "false_recovery_rate": false_recovery_rate,
            "dangerous_action_block_rate": dangerous_action_block_rate,
            "forbidden_execution_total": forbidden_execution_total,
            "avg_detection_time_seconds": avg_detection_time_seconds,
            "avg_remediation_time_seconds": avg_remediation_time_seconds,
            "rollback_success_rate": rollback_success_rate,
            "audit_completeness": audit_completeness,
            "verifier_authority_rate": verifier_authority_rate,
        }
