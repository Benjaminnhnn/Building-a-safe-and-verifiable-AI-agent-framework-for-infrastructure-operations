from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "automation" / "moodle-drill-summary.py"
spec = importlib.util.spec_from_file_location("moodle_drill_summary", SCRIPT)
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def test_summary_uses_measured_rto_and_probe_rate() -> None:
    evidence = {
        "status": "passed", "scenario_id": "CON-01", "run_id": "CON-01-run", "alert_resolved": "passed",
        "runtime_status": {"status": "resolved"},
        "stage_times": {"t_detect": "2026-11-07T10:00:00Z", "t_resolved": "2026-11-07T10:03:05Z"},
        "verifier": {"probe_samples": [{"checks": {"allowed": True, "forbidden": True}}] * 5},
    }
    text = summary.format_summary(evidence)
    assert "RESOLVED (1/1 sự cố)" in text
    assert "5/5 mẫu đạt (100%)" in text and "185 giây" in text
    assert "MoodleWebContainerMissing" in text and "đã hết firing" in text
    assert "RPO: không áp dụng" in text


def test_summary_rejects_unverified_recovery() -> None:
    try:
        summary.format_summary({"status": "passed", "runtime_status": {"status": "verifying"}})
    except ValueError:
        pass
    else:
        raise AssertionError("unverified incident must not be reported as recovered")
