"""Benchmark harness: metrics recorder and run configuration.

Supports three methods: manual, ansible, ai_agent.
Records all timestamps and metrics required by PLANNING.md Section 8.
"""

from __future__ import annotations

import json
import errno
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)


_RECORDER_LOCKS: dict[str, threading.Lock] = {}
_RECORDER_LOCKS_GUARD = threading.Lock()


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
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("All timestamps must be timezone-aware (tzinfo must not be None).")
        return v

    @model_validator(mode="after")
    def _ensure_chronological_order(self) -> RunTimestamps:
        ordered_fields = (
            "t_inject",
            "t_detect",
            "t_incident",
            "t_plan",
            "t_gate",
            "t_execute_start",
            "t_execute_end",
            "t_verify",
            "t_resolved",
        )
        previous_name: str | None = None
        previous_value: datetime | None = None
        for name in ordered_fields:
            value = getattr(self, name)
            if value is None:
                continue
            if previous_value is not None and value < previous_value:
                raise ValueError(
                    f"{name} must not precede earlier timestamp {previous_name}"
                )
            previous_name, previous_value = name, value
        return self

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
    """Record from the synthetic harness; this schema is not empirical evidence."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str
    method: BenchmarkMethod
    repetition: StrictInt = Field(ge=1)
    timestamps: RunTimestamps
    data_classification: Literal["synthetic_simulation_not_empirical"] = (
        "synthetic_simulation_not_empirical"
    )

    # Correctness metrics
    rca_correct: StrictBool | None = None
    recovery_success: StrictBool | None = None
    false_recovery: StrictBool | None = None  # health said ok but contract failed
    dangerous_action_blocked: StrictBool | None = None
    forbidden_execution_count: StrictInt = Field(default=0, ge=0)
    rollback_success: StrictBool | None = None

    # Efficiency metrics
    action_count: StrictInt = Field(default=0, ge=0)
    llm_call_count: StrictInt = Field(default=0, ge=0)
    llm_latency_seconds: float | None = Field(default=None, ge=0)
    runtime_seconds: float | None = Field(default=None, ge=0)
    aws_cost_usd: float | None = Field(default=None, ge=0)

    # Run provenance. Optional fields represent unavailable measurements as absent.
    commit_sha: str | None = None
    model_name: str | None = None
    model_configuration_sha256: str | None = None
    model_configuration_ref: str | None = None
    prompt_version: str | None = None
    environment: str | None = None
    image_digests: dict[str, str] = Field(default_factory=dict)

    # Verifier metrics
    audit_complete: StrictBool | None = None
    verifier_resolved: StrictBool | None = None  # only verifier set RESOLVED

    notes: str = ""

    @field_validator("run_id", "scenario_id")
    @classmethod
    def _require_nonblank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("run_id and scenario_id must not be blank")
        return value

    @field_validator("llm_latency_seconds", "runtime_seconds", "aws_cost_usd", mode="before")
    @classmethod
    def _require_finite_nonnegative_measure(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("measurements must be finite nonnegative numbers")
        if not math.isfinite(value) or value < 0:
            raise ValueError("measurements must be finite nonnegative numbers")
        return float(value)

    @field_validator("commit_sha")
    @classmethod
    def _validate_commit_sha(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise ValueError("commit_sha must be a full 40-character hexadecimal SHA")
        return value.lower() if value is not None else None

    @field_validator("model_name", "prompt_version", "environment")
    @classmethod
    def _require_nonblank_provenance(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("provenance fields must not be blank when provided")
        return value

    @field_validator("model_configuration_sha256")
    @classmethod
    def _validate_model_configuration_sha256(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("model_configuration_sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("model_configuration_ref")
    @classmethod
    def _validate_model_configuration_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model_configuration_ref must not be blank when provided")
        return value

    @model_validator(mode="after")
    def _require_complete_model_provenance(self) -> "RunResult":
        model_fields = (
            self.model_name,
            self.model_configuration_sha256,
            self.model_configuration_ref,
            self.prompt_version,
        )
        if any(value is not None for value in model_fields) and any(
            value is None for value in model_fields
        ):
            raise ValueError("model provenance requires name, config digest/reference, and prompt version")
        if (
            self.llm_call_count == 0
            and self.llm_latency_seconds is not None
            and self.llm_latency_seconds != 0
        ):
            raise ValueError("nonzero LLM latency requires at least one recorded LLM call")
        return self

    @field_validator("image_digests")
    @classmethod
    def _validate_image_digests(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not name.strip() or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
               for name, digest in value.items()):
            raise ValueError("image_digests must map nonblank image names to sha256 digests")
        return value


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
        self._lock_path = self._output_dir / f"{self._FILENAME}.lock"
        lock_key = str(self._path.resolve())
        with _RECORDER_LOCKS_GUARD:
            self._thread_lock = _RECORDER_LOCKS.setdefault(lock_key, threading.Lock())

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, result: RunResult) -> None:
        """Append *result* as a JSON line to the JSONL store."""
        if not isinstance(result, RunResult):
            raise TypeError("result must be a RunResult")
        result = RunResult.model_validate(result.model_dump(mode="python", warnings=False))
        with self._thread_lock, self._exclusive_file_lock():
            if any(existing.run_id == result.run_id for existing in self.load_all()):
                raise ValueError(f"duplicate benchmark run_id: {result.run_id}")
            line = result.model_dump_json() + "\n"
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    @contextmanager
    def _exclusive_file_lock(self) -> Iterator[None]:
        """Serialize duplicate-check plus append across independent processes."""
        with self._lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                # Lock byte zero without writing an initializer. Multiple
                # processes can create/open an empty lock file concurrently;
                # racing writes here caused Windows sharing violations.
                lock_file.seek(0)
                # LK_LOCK retries only a fixed number of times on Windows,
                # then raises PermissionError even when the peer is making
                # progress. Keep waiting like flock(LOCK_EX) does on POSIX.
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EDEADLK):
                            raise
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
                "data_classification": "synthetic_simulation_not_empirical",
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
                "avg_llm_latency_seconds": None,
                "total_runtime_seconds": None,
                "total_aws_cost_usd": None,
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

        llm_latencies = [r.llm_latency_seconds for r in results if r.llm_latency_seconds is not None]
        avg_llm_latency_seconds = (
            sum(llm_latencies) / len(llm_latencies) if llm_latencies else None
        )
        runtimes = [r.runtime_seconds for r in results if r.runtime_seconds is not None]
        total_runtime_seconds = sum(runtimes) if runtimes else None
        aws_costs = [r.aws_cost_usd for r in results if r.aws_cost_usd is not None]
        total_aws_cost_usd = sum(aws_costs) if aws_costs else None

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
            "data_classification": "synthetic_simulation_not_empirical",
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
            "avg_llm_latency_seconds": avg_llm_latency_seconds,
            "total_runtime_seconds": total_runtime_seconds,
            "total_aws_cost_usd": total_aws_cost_usd,
            "rollback_success_rate": rollback_success_rate,
            "audit_completeness": audit_completeness,
            "verifier_authority_rate": verifier_authority_rate,
        }
