#!/usr/bin/env python3
"""Render a static cost/operations dashboard from acceptance-ready main trials.

Incomplete data produces a status page with no metric totals. This tool does not
generate, estimate, or infer experiment results.
"""

from __future__ import annotations

import argparse
import html
import json
import runpy
from collections import defaultdict
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / "terraform" / ".artifacts"
ACCEPTANCE = runpy.run_path(str(REPO / "automation" / "sprint6-acceptance.py"))
METHODS = ("manual", "ansible", "ai_agent")


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate measured operational fields for each validated main method."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["method"]].append(row)
    return {
        method: {
            "trials": len(grouped[method]),
            "aws_cost_usd_total": sum(row["aws_cost_usd"] for row in grouped[method]),
            "runtime_seconds_total": sum(row["runtime_seconds"] for row in grouped[method]),
            "action_count_total": sum(row["action_count"] for row in grouped[method]),
            "llm_call_count_total": sum(row["llm_call_count"] for row in grouped[method]),
            "llm_latency_seconds_total": sum(row["llm_latency_seconds"] for row in grouped[method]),
            "recovery_successes": sum(row["recovery_success"] is True for row in grouped[method]),
            "run_ids": sorted(row["run_id"] for row in grouped[method]),
        }
        for method in METHODS
    }


def _provenance_html(audit: dict[str, Any]) -> str:
    provenance = audit.get("campaign_provenance", {})
    rows = []
    for field in ACCEPTANCE["PROVENANCE_FIELDS"]:
        values = provenance.get(field, [])
        value = values[0] if len(values) == 1 else "unavailable"
        rows.append(
            f"<tr><th>{html.escape(field)}</th><td><code>{html.escape(value)}</code></td></tr>"
        )
    return "<table><tbody>" + "".join(rows) + "</tbody></table>"


def render_html(
    audit: dict[str, Any],
    summaries: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render accepted measurements or an explicit no-data status page."""
    accepted = int(audit.get("accepted_empirical_runs", 0))
    required = int(audit.get("required_empirical_runs", 0))
    status = html.escape(str(audit.get("status", "incomplete")))
    page = [
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<title>Moodle benchmark cost and operations</title>",
        "<style>body{font:16px system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#20242a}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #ccd2d9;padding:.55rem;text-align:left}code{overflow-wrap:anywhere}.status{padding:.8rem;background:#f1f3f5;border-left:4px solid #697586}</style>",
        "<h1>Moodle benchmark cost and operations</h1>",
        f"<p class=\"status\">Acceptance status: <strong>{status}</strong>; empirical main rows accepted: {accepted} / {required}.</p>",
    ]
    if audit.get("status") != "ready":
        page.extend(
            [
                "<p>No cost, runtime, action, or LLM totals are shown because the main empirical matrix is not acceptance-ready.</p>",
                _provenance_html(audit),
                "</html>",
            ]
        )
        return "\n".join(page)

    if summaries is None:
        raise ValueError("accepted data requires method summaries")
    limitation = audit.get("sample_design", {}).get("limitation")
    if limitation:
        page.append(f"<p>Reduced-sample limitation: {html.escape(str(limitation))}</p>")
    page.append(_provenance_html(audit))
    page.append(
        "<table><thead><tr><th>Method</th><th>Trials</th><th>Measured AWS cost (USD)</th>"
        "<th>Runtime (seconds)</th><th>Actions</th><th>LLM calls</th>"
        "<th>LLM latency (seconds)</th><th>Verified recoveries</th></tr></thead><tbody>"
    )
    for method in METHODS:
        item = summaries[method]
        values = (
            method,
            str(item["trials"]),
            f"{item['aws_cost_usd_total']:.4f}",
            f"{item['runtime_seconds_total']:.1f}",
            str(item["action_count_total"]),
            str(item["llm_call_count_total"]),
            f"{item['llm_latency_seconds_total']:.3f}",
            f"{item['recovery_successes']} / {item['trials']}",
        )
        page.append("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in values) + "</tr>")
    page.append("</tbody></table>")
    page.append("<h2>Run traceability</h2>")
    for method in METHODS:
        page.append(f"<details><summary>{html.escape(method)} run IDs</summary><ul>")
        page.extend(f"<li><code>{html.escape(run_id)}</code></li>" for run_id in summaries[method]["run_ids"])
        page.append("</ul></details>")
    page.append("<p>Descriptive totals only; review raw evidence before thesis claims.</p></html>")
    return "\n".join(page)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ARTIFACTS / "sprint6-benchmark-live" / "benchmark_results.jsonl",
    )
    parser.add_argument("--repetitions", type=int, choices=(3, 5), default=5)
    parser.add_argument("--reduced-sample-limitation")
    parser.add_argument(
        "--output",
        type=Path,
        default=ARTIFACTS / "sprint6-benchmark-live" / "cost-dashboard.html",
    )
    args = parser.parse_args()
    audit = ACCEPTANCE["audit"](
        args.dataset, [], args.repetitions, args.reduced_sample_limitation
    )
    summaries = None
    if audit["status"] == "ready":
        rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
        summaries = summarize(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(audit, summaries), encoding="utf-8")
    print(
        f"Benchmark dashboard: {audit['status']}; empirical "
        f"{audit['accepted_empirical_runs']}/{audit['required_empirical_runs']}; {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
