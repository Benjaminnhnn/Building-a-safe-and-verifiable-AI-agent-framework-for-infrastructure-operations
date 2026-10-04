#!/usr/bin/env python3
"""Collect benchmark artifacts without inventing missing observations.

JSONL emitted by ``automation/run-benchmark.py`` is labeled simulated because
that runner uses fixture truth and RNG-derived outcomes. Sprint 2 ``result.json``
artifacts are labeled staging_runtime_shadow and explicitly retain that the
test harness, not the AI, performed the reset.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from evaluation.benchmark.benchmark_config import RunResult  # noqa: E402

FIELDS = [
    "run_id", "scenario_id", "method", "repetition", "evidence_class",
    "source_file", "status", "t_inject", "t_detect", "t_ai_observed",
    "t_execute_start", "t_execute_end", "t_verify", "t_resolved",
    "recovery_success", "false_recovery", "rca_correct",
    "dangerous_action_blocked", "forbidden_execution_count", "rollback_success",
    "action_count", "llm_call_count", "audit_complete", "verifier_resolved",
    "ai_layer_mode", "ai_execution", "execution_authority",
    "ai_shadow_observed", "stability_seconds", "mttd_seconds",
    "recovery_seconds", "completed_at", "prometheus_alert", "observation",
    "pipeline_report", "action", "mutated", "notes",
]


class InputError(ValueError):
    pass


def _benchmark_row(payload: dict[str, Any], source: Path, method: str | None) -> dict[str, Any]:
    result = RunResult.model_validate(payload)
    actual_method = result.method.value
    if method and actual_method != method:
        return {}
    timestamps = result.timestamps.model_dump(mode="json")
    row = result.model_dump(mode="json")
    row.pop("timestamps", None)
    row.update(timestamps)
    row["evidence_class"] = "simulated_fixture_benchmark"
    row["source_file"] = str(source)
    return {field: ("" if row.get(field) is None else row.get(field, "")) for field in FIELDS}


def _runtime_row(payload: dict[str, Any], source: Path, method: str) -> dict[str, Any]:
    required = ("run_id", "scenario_id", "status")
    if any(not payload.get(key) for key in required):
        raise InputError(f"{source}: runtime result is missing one of {required}")
    evidence_class = payload.get("evidence_class")
    if evidence_class not in {"staging_runtime_observed", "staging_runtime_shadow", "staging_runtime_manual_trial"}:
        raise InputError(f"{source}: missing a recognized runtime evidence_class from the trial producer")
    actual_method = payload.get("method")
    if actual_method not in {"ai_agent_shadow", "manual", "ansible"}:
        raise InputError(f"{source}: runtime trial has no recognized method label")
    if method and actual_method != method:
        return {}
    row = {
        key: payload.get(key, "")
        for key in FIELDS
    }
    row.update(
        method=actual_method,
        evidence_class=evidence_class,
        source_file=str(source),
        recovery_success="",
        ai_execution=payload.get("ai_execution", False),
        execution_authority=payload.get("execution_authority", ""),
        notes="Runtime observation only; does not imply AI executed remediation.",
    )
    for field in ("t_inject", "t_detect", "t_ai_observed", "t_execute_start", "t_execute_end", "t_verify", "t_resolved"):
        if payload.get(field):
            row[field] = payload[field]
    return row


def collect_campaign(campaign_dir: Path, method: str | None) -> list[dict[str, Any]]:
    if not campaign_dir.is_dir():
        raise InputError(f"campaign directory does not exist: {campaign_dir}")
    rows: list[dict[str, Any]] = []
    for source in sorted(campaign_dir.rglob("*")):
        if not source.is_file():
            continue
        if source.name == "benchmark_results.jsonl":
            with source.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = _benchmark_row(json.loads(line), source, method)
                    except Exception as exc:
                        raise InputError(f"{source}:{line_number}: invalid benchmark record: {exc}") from exc
                    if row:
                        rows.append(row)
        elif source.name == "result.json":
            try:
                payload = json.loads(source.read_text(encoding="utf-8-sig"))
                row = _runtime_row(payload, source, method or "")
                if row:
                    rows.append(row)
            except Exception as exc:
                raise InputError(f"{source}: invalid runtime result: {exc}") from exc
    if not rows:
        raise InputError(f"no supported result.json or benchmark_results.jsonl artifacts found under {campaign_dir}")
    return rows


def merge_csv(inputs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in inputs:
        if not path.is_file():
            raise InputError(f"input CSV does not exist: {path}")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not {"run_id", "scenario_id", "method", "evidence_class"}.issubset(reader.fieldnames):
                raise InputError(f"{path}: missing required identity/provenance columns")
            for line_number, row in enumerate(reader, 2):
                identity = (row["method"], row["run_id"])
                if not row["run_id"] or not row["scenario_id"] or not row["evidence_class"] or identity in seen:
                    raise InputError(f"{path}:{line_number}: empty or duplicate method/run_id {identity}")
                seen.add(identity)
                rows.append({field: row.get(field, "") for field in FIELDS})
    return rows


def write_csv(output: Path, rows: list[dict[str, Any]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path)
    parser.add_argument("--method", choices=("ai_agent", "ai-agent", "ai_agent_shadow", "manual", "ansible"))
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.merge:
            if args.campaign_dir or not args.inputs:
                parser.error("--merge requires --inputs and cannot be combined with --campaign-dir")
            rows = merge_csv(args.inputs)
        else:
            if not args.campaign_dir or args.inputs:
                parser.error("campaign mode requires --campaign-dir and cannot use --inputs")
            method = args.method.replace("-", "_") if args.method else None
            rows = collect_campaign(args.campaign_dir, method)
        identities: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row["method"]), str(row["run_id"]))
            if key in identities:
                raise InputError(f"duplicate method/run_id across campaign: {key}")
            identities.add(key)
        write_csv(args.output, rows)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {len(rows)} rows to {args.output}")
    for evidence_class in sorted({row["evidence_class"] for row in rows}):
        count = sum(row["evidence_class"] == evidence_class for row in rows)
        print(f"  {evidence_class}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
