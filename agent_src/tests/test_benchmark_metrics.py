"""Tests for benchmark metrics recorder and ablation configuration.

Run with:
    $env:PYTHONPATH="agent_src"; python -m pytest agent_src/tests/test_benchmark_metrics.py --basetemp=".pytest-tmp" -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the evaluation/ package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evaluation"))

import pytest

from benchmark.benchmark_config import (
    BenchmarkMethod,
    MetricsRecorder,
    RunResult,
    RunTimestamps,
)
from benchmark.ablation_config import (
    DEFAULT_ABLATION_SCENARIOS,
    AblationMode,
    NoSafetyGateConfig,
    NoVerifierConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TZ = timezone.utc


def _ts(**kwargs) -> RunTimestamps:
    """Build a RunTimestamps with only the supplied fields set (all UTC)."""
    return RunTimestamps(**kwargs)


def _make_result(
    run_id: str = "run-001",
    scenario_id: str = "DB-01",
    method: BenchmarkMethod = BenchmarkMethod.AI_AGENT,
    repetition: int = 1,
    timestamps: RunTimestamps | None = None,
    **kwargs,
) -> RunResult:
    if timestamps is None:
        timestamps = RunTimestamps()
    return RunResult(
        run_id=run_id,
        scenario_id=scenario_id,
        method=method,
        repetition=repetition,
        timestamps=timestamps,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Detection and remediation time computed from timestamps
# ---------------------------------------------------------------------------


def test_run_result_timestamps_computed() -> None:
    """detection_time and remediation_time are derived from timestamps."""
    t_inject = datetime(2025, 1, 1, 10, 0, 0, tzinfo=_TZ)
    t_detect = datetime(2025, 1, 1, 10, 0, 45, tzinfo=_TZ)
    t_execute_start = datetime(2025, 1, 1, 10, 1, 0, tzinfo=_TZ)
    t_resolved = datetime(2025, 1, 1, 10, 6, 0, tzinfo=_TZ)

    ts = _ts(
        t_inject=t_inject,
        t_detect=t_detect,
        t_execute_start=t_execute_start,
        t_resolved=t_resolved,
    )

    assert ts.detection_time_seconds == pytest.approx(45.0)
    assert ts.remediation_time_seconds == pytest.approx(300.0)


def test_run_result_timestamps_none_when_fields_missing() -> None:
    """Properties return None when required fields are absent."""
    ts = RunTimestamps()
    assert ts.detection_time_seconds is None
    assert ts.remediation_time_seconds is None


def test_timestamps_timezone_aware_required() -> None:
    """Naive datetimes must be rejected."""
    naive = datetime(2025, 1, 1, 10, 0, 0)  # no tzinfo
    with pytest.raises(Exception):
        RunTimestamps(t_inject=naive)


# ---------------------------------------------------------------------------
# 2. MetricsRecorder: append and load
# ---------------------------------------------------------------------------


def test_metrics_recorder_append_and_load(tmp_path: Path) -> None:
    """Record 3 results, load all, verify count and summary correctness."""
    recorder = MetricsRecorder(tmp_path)

    t_inject = datetime(2025, 1, 1, 10, 0, 0, tzinfo=_TZ)
    t_detect = datetime(2025, 1, 1, 10, 1, 0, tzinfo=_TZ)
    t_exe = datetime(2025, 1, 1, 10, 2, 0, tzinfo=_TZ)
    t_res = datetime(2025, 1, 1, 10, 7, 0, tzinfo=_TZ)

    base_ts = _ts(t_inject=t_inject, t_detect=t_detect, t_execute_start=t_exe, t_resolved=t_res)

    results = [
        _make_result(run_id=f"run-{i}", scenario_id="DB-01", repetition=i, timestamps=base_ts)
        for i in range(1, 4)
    ]
    for r in results:
        recorder.record(r)

    loaded = recorder.load_all()
    assert len(loaded) == 3
    assert [r.run_id for r in loaded] == ["run-1", "run-2", "run-3"]


def test_metrics_recorder_summary_counts(tmp_path: Path) -> None:
    """Summary total_runs / passed / failed counts are correct."""
    recorder = MetricsRecorder(tmp_path)

    recorder.record(_make_result(run_id="a", recovery_success=True))
    recorder.record(_make_result(run_id="b", recovery_success=True))
    recorder.record(_make_result(run_id="c", recovery_success=False))

    s = recorder.summary()
    assert s["total_runs"] == 3
    assert s["passed"] == 2
    assert s["failed"] == 1


# ---------------------------------------------------------------------------
# 3. RCA accuracy
# ---------------------------------------------------------------------------


def test_summary_rca_accuracy(tmp_path: Path) -> None:
    """2/3 correct → accuracy ≈ 0.667."""
    recorder = MetricsRecorder(tmp_path)

    recorder.record(_make_result(run_id="a", rca_correct=True))
    recorder.record(_make_result(run_id="b", rca_correct=True))
    recorder.record(_make_result(run_id="c", rca_correct=False))

    s = recorder.summary()
    assert s["rca_accuracy"] == pytest.approx(2 / 3, abs=0.01)


def test_summary_rca_accuracy_none_excluded(tmp_path: Path) -> None:
    """Runs with rca_correct=None are excluded from the accuracy denominator."""
    recorder = MetricsRecorder(tmp_path)

    recorder.record(_make_result(run_id="a", rca_correct=True))
    recorder.record(_make_result(run_id="b", rca_correct=None))  # excluded

    s = recorder.summary()
    assert s["rca_accuracy"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 4. Forbidden execution total
# ---------------------------------------------------------------------------


def test_summary_forbidden_execution_total(tmp_path: Path) -> None:
    """forbidden_execution_total is the sum across all runs."""
    recorder = MetricsRecorder(tmp_path)

    recorder.record(_make_result(run_id="a", forbidden_execution_count=0))
    recorder.record(_make_result(run_id="b", forbidden_execution_count=2))
    recorder.record(_make_result(run_id="c", forbidden_execution_count=1))

    s = recorder.summary()
    assert s["forbidden_execution_total"] == 3


# ---------------------------------------------------------------------------
# 5. Ablation config – no_safety_gate
# ---------------------------------------------------------------------------


def test_ablation_config_no_safety_gate() -> None:
    """NoSafetyGateConfig has shadow_only=True and mode=NO_SAFETY_GATE."""
    cfg = NoSafetyGateConfig()
    assert cfg.shadow_only is True
    assert cfg.mode == AblationMode.NO_SAFETY_GATE
    assert cfg.min_repetitions >= 5
    assert len(cfg.scenarios) > 0


# ---------------------------------------------------------------------------
# 6. Ablation config – no_verifier
# ---------------------------------------------------------------------------


def test_ablation_config_no_verifier() -> None:
    """NoVerifierConfig has counterfactual_only=True and mode=NO_VERIFIER."""
    cfg = NoVerifierConfig()
    assert cfg.counterfactual_only is True
    assert cfg.mode == AblationMode.NO_VERIFIER
    assert cfg.min_repetitions >= 5


# ---------------------------------------------------------------------------
# 7. All ablation scenarios are valid Moodle IDs
# ---------------------------------------------------------------------------

_KNOWN_MOODLE_IDS = {
    "CON-01", "CON-02", "CON-03",
    "DB-01", "DB-02", "DB-03",
    "NET-01", "NET-02", "NET-03",
    "RES-01", "RES-02", "RES-03",
    "SEC-01", "SEC-02", "SEC-03",
}


def test_all_ablation_scenarios_are_valid_moodle_ids() -> None:
    """DEFAULT_ABLATION_SCENARIOS must be a subset of the 15 known Moodle IDs."""
    assert set(DEFAULT_ABLATION_SCENARIOS).issubset(_KNOWN_MOODLE_IDS), (
        f"Unknown scenario IDs: {set(DEFAULT_ABLATION_SCENARIOS) - _KNOWN_MOODLE_IDS}"
    )
    # Both factory functions must also use only valid IDs
    for cfg in (NoSafetyGateConfig(), NoVerifierConfig()):
        unknown = set(cfg.scenarios) - _KNOWN_MOODLE_IDS
        assert not unknown, f"Ablation config {cfg.mode} has unknown IDs: {unknown}"


# ---------------------------------------------------------------------------
# 8. False recovery rate in summary
# ---------------------------------------------------------------------------


def test_false_recovery_rate_in_summary(tmp_path: Path) -> None:
    """A run with false_recovery=True increments the false recovery rate."""
    recorder = MetricsRecorder(tmp_path)

    recorder.record(_make_result(run_id="a", false_recovery=False))
    recorder.record(_make_result(run_id="b", false_recovery=False))
    recorder.record(_make_result(run_id="c", false_recovery=True))

    s = recorder.summary()
    # 1 out of 3 runs has false_recovery=True → rate ≈ 0.333
    assert s["false_recovery_rate"] == pytest.approx(1 / 3, abs=0.01)


def test_false_recovery_rate_none_excluded(tmp_path: Path) -> None:
    """Runs with false_recovery=None are excluded from the rate denominator."""
    recorder = MetricsRecorder(tmp_path)

    recorder.record(_make_result(run_id="a", false_recovery=True))
    recorder.record(_make_result(run_id="b", false_recovery=None))  # excluded

    s = recorder.summary()
    # 1/1 verdicted run has false_recovery=True
    assert s["false_recovery_rate"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 9. Extra field forbidden (Pydantic extra="forbid")
# ---------------------------------------------------------------------------


def test_run_result_extra_fields_forbidden() -> None:
    """RunResult must reject unexpected extra fields."""
    with pytest.raises(Exception):
        RunResult(
            run_id="x",
            scenario_id="DB-01",
            method=BenchmarkMethod.MANUAL,
            repetition=1,
            timestamps=RunTimestamps(),
            unexpected_field="boom",  # type: ignore[call-arg]
        )


def test_run_timestamps_extra_fields_forbidden() -> None:
    """RunTimestamps must reject unexpected extra fields."""
    with pytest.raises(Exception):
        RunTimestamps(extra_key="boom")  # type: ignore[call-arg]
