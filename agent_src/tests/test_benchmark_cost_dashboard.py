"""Cost dashboard must not turn absent empirical data into zero results."""

from __future__ import annotations

import runpy
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DASHBOARD = runpy.run_path(str(REPO / "automation" / "benchmark-cost-dashboard.py"))


def test_incomplete_dashboard_hides_metric_totals() -> None:
    page = DASHBOARD["render_html"](
        {
            "status": "incomplete",
            "accepted_empirical_runs": 0,
            "required_empirical_runs": 225,
            "campaign_provenance": {},
        }
    )

    assert "0 / 225" in page
    assert "No cost, runtime, action, or LLM totals are shown" in page
    assert "Measured AWS cost (USD)" not in page
    assert "0.0000" not in page


def test_summarize_aggregates_measured_rows_by_method() -> None:
    rows = [
        {
            "method": "manual",
            "run_id": "run-b",
            "aws_cost_usd": 0.25,
            "runtime_seconds": 12.5,
            "action_count": 1,
            "llm_call_count": 0,
            "llm_latency_seconds": 0.0,
            "recovery_success": True,
        },
        {
            "method": "manual",
            "run_id": "run-a",
            "aws_cost_usd": 0.75,
            "runtime_seconds": 10.0,
            "action_count": 2,
            "llm_call_count": 3,
            "llm_latency_seconds": 4.5,
            "recovery_success": False,
        },
    ]

    result = DASHBOARD["summarize"](rows)

    assert result["manual"] == {
        "trials": 2,
        "aws_cost_usd_total": 1.0,
        "runtime_seconds_total": 22.5,
        "action_count_total": 3,
        "llm_call_count_total": 3,
        "llm_latency_seconds_total": 4.5,
        "recovery_successes": 1,
        "run_ids": ["run-a", "run-b"],
    }
    assert result["ansible"]["trials"] == 0
    assert result["ai_agent"]["trials"] == 0
