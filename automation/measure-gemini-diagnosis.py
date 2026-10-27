#!/usr/bin/env python3
"""Measure the RCA diagnosis layer with the real Gemini model.

Runs the diagnosis ablation (rules, llm, llm_rag, hybrid, hybrid_rag,
llm_unvalidated) against synthetic fixtures with a live Gemini client and
writes one JSON report with accuracy, stability, robustness, token cost and
latency. It sends prompts to an external API and spends quota, so it does
nothing until confirmed:

    export GEMINI_API_KEY=...            # or --api-key-file <path>
    export AIOPS_GEMINI_MEASURE_CONFIRM=send-synthetic-fixtures
    python automation/measure-gemini-diagnosis.py --repeats 3 \\
        --usd-per-mtok-in <price> --usd-per-mtok-out <price>

Prompts contain only synthetic fixture evidence and the repository runbooks,
never production telemetry. The result measures the diagnosis layer offline;
it is not live recovery evidence and does not touch any infrastructure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.reasoning.llm_client import (  # noqa: E402
    DiagnosisLLM,
    ScriptedDiagnosisLLM,
    build_default_client,
    valid_env_value,
)
from core.reasoning.llm_diagnosis import SYSTEM_INSTRUCTION  # noqa: E402
from evaluation.benchmark import diagnosis_ablation as ablation  # noqa: E402

CONFIRM_VARIABLE = "AIOPS_GEMINI_MEASURE_CONFIRM"
CONFIRM_VALUE = "send-synthetic-fixtures"
MAX_REPEATS = 10
DEFAULT_OUTPUT_DIR = REPO_ROOT / "terraform" / ".artifacts" / "gemini-diagnosis"
LLM_CONDITIONS = ("llm", "llm_rag", "hybrid", "hybrid_rag", "llm_unvalidated")


def plan_calls(repeats: int, *, skip_robustness: bool) -> int:
    """Count the model calls a run would make, using the offline oracle."""

    def factory(case: ablation.DiagnosisCase) -> DiagnosisLLM:
        return ScriptedDiagnosisLLM(ablation.oracle_responder(case), model="scripted-oracle")

    rows = ablation.run_trials(client_factory=factory, repeats=repeats)
    if not skip_robustness:
        cases = tuple(case for case in ablation.CASES if case.expected_cause is not None)
        for kind in ablation.PERTURBATIONS:
            rows += ablation.run_trials(client_factory=factory, cases=cases, perturbation=kind)
    return sum(row["llm_call_count"] for row in rows)


def load_client(api_key_file: Path | None) -> DiagnosisLLM | None:
    """Build the Gemini client; the key is read from the environment or a file, never argv."""
    if api_key_file is not None:
        key = valid_env_value(api_key_file.read_text(encoding="utf-8").strip())
        if key is None:
            return None
        os.environ["GEMINI_API_KEY"] = key
    return build_default_client()


def llm_error_rows(rows: list[dict[str, Any]]) -> int:
    """Rows where the provider failed and the pipeline fell back, so no model answer was scored."""
    return sum(
        row["condition"] in LLM_CONDITIONS and row["source"] == "llm_unavailable" for row in rows
    )


def measure(args: argparse.Namespace, client: DiagnosisLLM) -> dict[str, Any]:
    def factory(_case: ablation.DiagnosisCase) -> DiagnosisLLM:
        return client

    accuracy = ablation.run_accuracy(
        client_factory=factory, repeats=args.repeats,
        usd_per_mtok_in=args.usd_per_mtok_in, usd_per_mtok_out=args.usd_per_mtok_out,
    )
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client": type(client).__name__,
        "model": client.model,
        "simulated": isinstance(client, ScriptedDiagnosisLLM),
        "evidence": "synthetic fixtures; offline diagnosis-layer evidence only, not live recovery",
        "repeats": args.repeats,
        "provenance": {
            "system_instruction_sha256": hashlib.sha256(SYSTEM_INSTRUCTION.encode("utf-8")).hexdigest(),
            "python": platform.python_version(),
            "prices_usd_per_mtok": {"input": args.usd_per_mtok_in, "output": args.usd_per_mtok_out},
        },
        "accuracy": accuracy,
        "adversarial": ablation.run_adversarial(),
    }
    if not args.skip_robustness:
        report["robustness"] = ablation.run_robustness(client_factory=factory, repeats=1)
    all_rows = accuracy["rows"] + report.get("robustness", {}).get("rows", [])
    report["provider_errors"] = llm_error_rows(all_rows)
    report["valid_model_measurement"] = report["provider_errors"] == 0 and not report["simulated"]
    return report


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        f"model={report['model']} repeats={report['repeats']} simulated={report['simulated']} "
        f"provider_errors={report['provider_errors']}",
        f"{'condition':<16}{'RCA acc':>9}{'false dx':>10}{'abstain':>9}{'leaks':>7}"
        f"{'calls':>7}{'tok in':>9}{'tok out':>9}{'mean s':>8}{'p95 s':>8}{'USD':>10}",
    ]
    for condition, stats in report["accuracy"]["summary"].items():
        cost = stats["cost"]

        def fmt(value: Any, spec: str = "") -> str:
            return "-" if value is None else format(value, spec)

        lines.append(
            f"{condition:<16}{fmt(stats['root_cause_accuracy'], '.2f'):>9}"
            f"{fmt(stats['false_diagnosis_rate'], '.2f'):>10}{fmt(stats['correct_abstain_rate'], '.2f'):>9}"
            f"{stats['secret_leaks']:>7}{cost['llm_call_count']:>7}{cost['prompt_tokens']:>9}"
            f"{cost['output_tokens']:>9}{fmt(cost['mean_latency_seconds'], '.2f'):>8}"
            f"{fmt(cost['p95_latency_seconds'], '.2f'):>8}{fmt(cost['estimated_usd'], '.4f'):>10}"
        )
    adversarial = report["adversarial"]["summary"]
    lines.append(
        "validator on scripted adversarial outputs (no model call): "
        + ", ".join(f"{mode} blocked {item['unsupported_diagnoses_blocked']}/{item['trials']}"
                    for mode, item in adversarial.items())
    )
    if not report["valid_model_measurement"]:
        lines.append(
            "WARNING: provider errors or a scripted client are present; do not report these as model quality."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure RCA diagnosis with the real Gemini model.")
    parser.add_argument("--repeats", type=int, default=3, help=f"1..{MAX_REPEATS} (default 3)")
    parser.add_argument("--max-calls", type=int, default=800, help="refuse to run above this planned call count")
    parser.add_argument("--api-key-file", type=Path, help="file holding the key; GEMINI_API_KEY is used otherwise")
    parser.add_argument("--usd-per-mtok-in", type=float, help="input price per million tokens; no cost without it")
    parser.add_argument("--usd-per-mtok-out", type=float, help="output price per million tokens")
    parser.add_argument("--skip-robustness", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    if not 1 <= args.repeats <= MAX_REPEATS:
        print(f"--repeats must be between 1 and {MAX_REPEATS}", file=sys.stderr)
        return 64
    if (args.usd_per_mtok_in is None) != (args.usd_per_mtok_out is None):
        print("pass both --usd-per-mtok-in and --usd-per-mtok-out, or neither", file=sys.stderr)
        return 64

    planned = plan_calls(args.repeats, skip_robustness=args.skip_robustness)
    print(f"Planned Gemini calls: about {planned} (cap {args.max_calls}); synthetic fixtures only.")
    if planned > args.max_calls:
        print("Planned calls exceed --max-calls; lower --repeats or raise the cap.", file=sys.stderr)
        return 64
    if os.environ.get(CONFIRM_VARIABLE) != CONFIRM_VALUE:
        print(f"Nothing was sent. Set {CONFIRM_VARIABLE}={CONFIRM_VALUE} to run.", file=sys.stderr)
        return 3

    try:
        client = load_client(args.api_key_file)
    except OSError as exc:
        print(f"cannot read the key file: {type(exc).__name__}", file=sys.stderr)
        return 2
    if client is None:
        print("No usable GEMINI_API_KEY (unset or placeholder); nothing was sent.", file=sys.stderr)
        return 2
    try:
        import google.genai  # noqa: F401
    except ImportError:
        print("google-genai is not installed; run pip install -r agent_src/requirements.txt", file=sys.stderr)
        return 2

    report = measure(args, client)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"gemini-diagnosis-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(render_summary(report))
    print(f"Report: {output}")
    return 0 if report["valid_model_measurement"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
