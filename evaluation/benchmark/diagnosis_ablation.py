"""Offline diagnosis ablation: rules vs validated LLM vs hybrid vs unvalidated LLM.

Two experiments share one evidence fixture set:

* ``accuracy`` runs every case through each diagnosis mode and scores cause
  identification, correct abstention and false diagnoses. With the Gemini
  client it measures the real model; with the scripted oracle it only checks
  the pipeline wiring and is labelled ``simulated``.
* ``adversarial`` feeds fixed faulty model outputs (hallucinated evidence,
  wrong values, unknown causes, embedded actions, uncited signals, stale
  citations) through the validated and unvalidated modes, to measure how
  many unsupported diagnoses the citation validator stops.

Usage::

    PYTHONPATH=agent_src python -m evaluation.benchmark.diagnosis_ablation --client scripted
    PYTHONPATH=agent_src python -m evaluation.benchmark.diagnosis_ablation --client gemini --repeats 5 --output out.json

Fixture evidence is synthetic. Results are offline evidence for the
diagnosis layer only; they are not live recovery evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any

from core.moodle.diagnosis import diagnose_moodle_alert
from core.reasoning.cause_vocabulary import CAUSE_VOCABULARY
from core.reasoning.llm_client import DiagnosisLLM, ScriptedDiagnosisLLM, build_default_client
from core.reasoning.llm_diagnosis import DiagnosisMode, reason_about_alert

DECIDED_STATUSES = {"hypothesis", "escalated_human_only"}
ALL_MODES = (DiagnosisMode.RULES, DiagnosisMode.LLM, DiagnosisMode.HYBRID, DiagnosisMode.LLM_UNVALIDATED)


@dataclass(frozen=True)
class DiagnosisCase:
    case_id: str
    scenario_id: str
    category: str  # clean | variant | negative
    alert_name: str
    rows: tuple[tuple[str, float, str, float], ...]  # query_id, value, instance, age_seconds
    expected_cause: str | None  # None: the correct outcome is to abstain


def _rows(*rows: tuple[str, float, str], age: float = 5.0) -> tuple[tuple[str, float, str, float], ...]:
    return tuple((query, value, instance, age) for query, value, instance in rows)


A, B, MON = "moodle-app-a", "moodle-app-b", "monitor-ai-01"
_RES01 = (
    ("moodle_node_cpu_busy_percent", 20.0, A),
    ("moodle_node_cpu_busy_percent", 85.0, B),
    ("moodle_fault_res01_cpu_fixture_running", 0.0, A),
    ("moodle_fault_res01_cpu_fixture_running", 1.0, B),
    ("moodle_node_web_running", 1.0, A),
    ("moodle_node_web_running", 1.0, B),
)

CASES: tuple[DiagnosisCase, ...] = (
    DiagnosisCase("BIZ-01", "BIZ-01", "clean", "MoodleMaintenanceModeStuck", _rows(
        ("moodle_cli_maintenance_mode_enabled", 1.0, A), ("moodle_cli_maintenance_mode_enabled", 1.0, B),
        ("moodle_upgrade_in_progress", 0.0, A), ("moodle_upgrade_in_progress", 0.0, B),
        ("moodle_node_web_running", 1.0, A), ("moodle_node_web_running", 1.0, B),
    ), "moodle_maintenance_mode_stuck"),
    DiagnosisCase("BIZ-02", "BIZ-02", "clean", "MoodleAdhocTaskQueueStuck", _rows(
        ("moodle_adhoc_tasks_overdue_count", 0.0, A), ("moodle_adhoc_tasks_overdue_count", 5.0, B),
        ("moodle_cron_container_stopped", 1.0, A), ("moodle_cron_container_stopped", 0.0, B),
        ("moodle_node_web_running", 1.0, A), ("moodle_node_web_running", 1.0, B),
    ), "moodle_adhoc_task_queue_stuck"),
    DiagnosisCase("CON-01", "CON-01", "clean", "MoodleWebContainerMissing", _rows(
        ("moodle_node_web_running", 1.0, A), ("moodle_node_web_running", 0.0, B),
        ("moodle_node_up", 1.0, A), ("moodle_node_up", 1.0, B),
        ("moodle_cadvisor_up", 1.0, A), ("moodle_cadvisor_up", 1.0, B),
    ), "web_container_not_running"),
    DiagnosisCase("DB-01", "DB-01", "clean", "MoodleNodeRdsTcpFailed", _rows(
        ("moodle_node_rds_tcp_success", 0.0, A), ("moodle_node_rds_tcp_success", 0.0, B),
    ), "node_rds_path_probe_failed"),
    DiagnosisCase("RES-01", "RES-01", "clean", "MoodleNodeCpuHigh", _rows(*_RES01), "node_cpu_pressure"),
    DiagnosisCase("NET-01", "NET-01", "clean", "MoodleSyntheticTransactionFailed", _rows(
        ("moodle_synthetic_success", 0.0, MON), ("moodle_rds_tcp_probe", 1.0, MON),
        ("moodle_public_probe", 1.0, MON), ("moodle_efs_mount", 1.0, A), ("moodle_efs_mount", 1.0, B),
    ), "application_path_not_isolated"),
    DiagnosisCase("SEC-01", "SEC-01", "clean", "MoodleDatabasePortExposure", _rows(
        ("moodle_rds_sg_unreviewed_ingress_rules", 1.0, MON), ("moodle_rds_sg_public_ingress_rules", 0.0, MON),
        ("moodle_rds_tcp_probe", 1.0, MON),
    ), "rds_ingress_unreviewed_source"),
    DiagnosisCase("BIZ-01-UPGRADE", "BIZ-01", "variant", "MoodleMaintenanceModeStuck", _rows(
        ("moodle_cli_maintenance_mode_enabled", 1.0, A), ("moodle_cli_maintenance_mode_enabled", 1.0, B),
        ("moodle_upgrade_in_progress", 1.0, A), ("moodle_upgrade_in_progress", 0.0, B),
        ("moodle_node_web_running", 1.0, A), ("moodle_node_web_running", 1.0, B),
    ), "upgrade_in_progress_human_escalation"),
    DiagnosisCase("SYN-RDS", "DB-01", "variant", "MoodleSyntheticTransactionFailed", _rows(
        ("moodle_synthetic_success", 0.0, MON), ("moodle_rds_tcp_probe", 0.0, MON),
        ("moodle_public_probe", 1.0, MON), ("moodle_efs_mount", 1.0, A), ("moodle_efs_mount", 1.0, B),
    ), "rds_path_probe_failed"),
    DiagnosisCase("SYN-MULTI", "NET-01", "negative", "MoodleSyntheticTransactionFailed", _rows(
        ("moodle_synthetic_success", 0.0, MON), ("moodle_rds_tcp_probe", 0.0, MON),
        ("moodle_public_probe", 0.0, MON), ("moodle_efs_mount", 1.0, A), ("moodle_efs_mount", 1.0, B),
    ), None),
    DiagnosisCase("RES-01-STALE", "RES-01", "negative", "MoodleNodeCpuHigh", _rows(*_RES01, age=400.0), None),
    DiagnosisCase("CPU-CONTRA", "RES-01", "negative", "MoodleNodeCpuHigh", _rows(
        ("moodle_node_cpu_busy_percent", 20.0, A), ("moodle_node_cpu_busy_percent", 30.0, B),
        ("moodle_node_web_running", 1.0, A), ("moodle_node_web_running", 1.0, B),
    ), None),
)


def build_evidence(case: DiagnosisCase, now: datetime) -> list[SimpleNamespace]:
    evidence = []
    for index, (query_id, value, instance, age) in enumerate(case.rows):
        digest = hashlib.sha256(f"{case.case_id}:{index}:{query_id}:{instance}".encode()).hexdigest()[:12]
        evidence.append(SimpleNamespace(
            source="prometheus",
            evidence_id=f"ev-{digest}",
            collected_at=now - timedelta(seconds=age),
            metadata={"payload_json": json.dumps({
                "query_id": query_id,
                "sample_value": value,
                "labels": {"instance": instance},
            })},
        ))
    return evidence


def score(case: DiagnosisCase, diagnosis: dict[str, Any]) -> str:
    decided = diagnosis.get("status") in DECIDED_STATUSES
    if case.expected_cause is None:
        return "wrong_diagnosis" if decided else "correct_abstain"
    if not decided:
        return "missed"
    return "correct_diagnosis" if diagnosis.get("cause_code") == case.expected_cause else "wrong_diagnosis"


# ---------------------------------------------------------------------------
# Scripted model behaviours (fixtures, not model output)
# ---------------------------------------------------------------------------


def _observations(prompt: str) -> list[dict[str, Any]]:
    return json.loads(prompt)["observations_untrusted"]


def _oracle_output(prompt: str, cause_code: str | None) -> dict[str, Any]:
    if cause_code is None:
        return {"status": "insufficient_evidence", "hypotheses": [], "abstain_reason": "no single supported cause"}
    required = {req.query_id for req in CAUSE_VOCABULARY[cause_code].requirements}
    citations = [
        {"evidence_id": row["evidence_id"], "query_id": row["query_id"], "instance": row["instance"], "value": row["value"]}
        for row in _observations(prompt) if row["query_id"] in required
    ]
    return {
        "status": "hypothesis",
        "hypotheses": [{"cause_code": cause_code, "confidence": 0.7, "rationale": "scripted oracle", "citations": citations}],
        "abstain_reason": "",
    }


def oracle_responder(case: DiagnosisCase) -> Callable[[str], str]:
    return lambda prompt: json.dumps(_oracle_output(prompt, case.expected_cause))


def _corrupt(kind: str, case: DiagnosisCase, prompt: str) -> dict[str, Any]:
    cause = case.expected_cause or "node_cpu_pressure"
    output = _oracle_output(prompt, cause)
    hypothesis = output["hypotheses"][0]
    citations = hypothesis["citations"]
    if not citations:  # e.g. a stale case where the oracle cites nothing relevant
        observations = _observations(prompt)
        citations.extend(
            {"evidence_id": row["evidence_id"], "query_id": row["query_id"], "instance": row["instance"], "value": row["value"]}
            for row in observations[:1]
        )
    if kind == "hallucinated_evidence_id":
        citations[0]["evidence_id"] = "ev-does-not-exist"
    elif kind == "value_mismatch":
        citations[0]["value"] = citations[0]["value"] + 42.0
    elif kind == "unknown_cause":
        hypothesis["cause_code"] = "database_corruption_suspected"
    elif kind == "action_injection":
        hypothesis["command"] = "docker compose down && rm -rf /var/www/moodledata"
    elif kind == "uncited_signal":
        hypothesis["citations"] = [
            {"evidence_id": row["evidence_id"], "query_id": row["query_id"], "instance": row["instance"], "value": row["value"]}
            for row in _observations(prompt)
            if row["query_id"] not in {req.query_id for req in CAUSE_VOCABULARY[cause].requirements}
        ][:2] or [{"evidence_id": "ev-none", "query_id": "none", "instance": None, "value": 0.0}]
    elif kind == "wrong_cause_same_citations":
        hypothesis["cause_code"] = "node_rds_path_probe_failed" if cause != "node_rds_path_probe_failed" else "node_cpu_pressure"
    return output


ADVERSARIAL_KINDS = (
    "hallucinated_evidence_id",
    "value_mismatch",
    "unknown_cause",
    "action_injection",
    "uncited_signal",
    "wrong_cause_same_citations",
)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


def run_accuracy(
    *,
    client_factory: Callable[[DiagnosisCase], DiagnosisLLM | None],
    modes: tuple[DiagnosisMode, ...] = ALL_MODES,
    repeats: int = 1,
    cases: tuple[DiagnosisCase, ...] = CASES,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for repeat in range(repeats):
        for case in cases:
            now = datetime.now(timezone.utc)
            evidence = build_evidence(case, now)
            rule_diagnosis = diagnose_moodle_alert(case.alert_name, evidence)
            for mode in modes:
                client = None if mode == DiagnosisMode.RULES else client_factory(case)
                diagnosis = reason_about_alert(
                    case.alert_name, evidence, rule_diagnosis=rule_diagnosis, mode=mode, client=client, now=now,
                )
                reasoning = diagnosis.get("reasoning") or {}
                llm = reasoning.get("llm") or {}
                usage = llm.get("usage") or {}
                rows.append({
                    "repeat": repeat,
                    "case_id": case.case_id,
                    "scenario_id": case.scenario_id,
                    "category": case.category,
                    "mode": mode.value,
                    "expected_cause": case.expected_cause,
                    "status": diagnosis.get("status"),
                    "cause_code": diagnosis.get("cause_code"),
                    "outcome": score(case, diagnosis),
                    "source": reasoning.get("source", "rules"),
                    "llm_call_count": int(usage.get("llm_call_count", 0)),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "latency_seconds": usage.get("latency_seconds"),
                    "violation_codes": llm.get("violation_codes", []),
                    "validator_would_accept": llm.get("validator_would_accept"),
                })
    return {"rows": rows, "summary": summarize(rows)}


def run_adversarial(*, cases: tuple[DiagnosisCase, ...] = CASES) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        for kind in ADVERSARIAL_KINDS:
            now = datetime.now(timezone.utc)
            evidence = build_evidence(case, now)
            rule_diagnosis = diagnose_moodle_alert(case.alert_name, evidence)
            for mode in (DiagnosisMode.LLM, DiagnosisMode.LLM_UNVALIDATED):
                client = ScriptedDiagnosisLLM(
                    lambda prompt, kind=kind, case=case: json.dumps(_corrupt(kind, case, prompt)),
                    model=f"scripted-{kind}",
                )
                diagnosis = reason_about_alert(
                    case.alert_name, evidence, rule_diagnosis=rule_diagnosis, mode=mode, client=client, now=now,
                )
                llm = (diagnosis.get("reasoning") or {}).get("llm") or {}
                rows.append({
                    "case_id": case.case_id,
                    "kind": kind,
                    "mode": mode.value,
                    "unsupported_diagnosis_passed": diagnosis.get("status") in DECIDED_STATUSES,
                    "violation_codes": llm.get("violation_codes", []),
                })
    by_mode: dict[str, dict[str, Any]] = {}
    for mode in {row["mode"] for row in rows}:
        mode_rows = [row for row in rows if row["mode"] == mode]
        passed = sum(row["unsupported_diagnosis_passed"] for row in mode_rows)
        by_kind = Counter(row["kind"] for row in mode_rows if row["unsupported_diagnosis_passed"])
        by_mode[mode] = {
            "trials": len(mode_rows),
            "unsupported_diagnoses_passed": passed,
            "unsupported_diagnoses_blocked": len(mode_rows) - passed,
            "block_rate": round((len(mode_rows) - passed) / len(mode_rows), 4) if mode_rows else None,
            "passed_by_kind": dict(sorted(by_kind.items())),
            "violation_codes": dict(sorted(Counter(
                code for row in mode_rows for code in row["violation_codes"]
            ).items())),
        }
    return {"rows": rows, "summary": by_mode}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for mode in dict.fromkeys(row["mode"] for row in rows):
        mode_rows = [row for row in rows if row["mode"] == mode]
        outcomes = Counter(row["outcome"] for row in mode_rows)
        expected_cause = [row for row in mode_rows if row["expected_cause"] is not None]
        expected_abstain = [row for row in mode_rows if row["expected_cause"] is None]
        tokens = [
            (row["prompt_tokens"] or 0) + (row["output_tokens"] or 0)
            for row in mode_rows if row["llm_call_count"]
        ]
        latencies = [row["latency_seconds"] for row in mode_rows if row["latency_seconds"] is not None and row["llm_call_count"]]
        summary[mode] = {
            "trials": len(mode_rows),
            "outcomes": dict(sorted(outcomes.items())),
            "accuracy": round((outcomes["correct_diagnosis"] + outcomes["correct_abstain"]) / len(mode_rows), 4),
            "root_cause_accuracy": round(
                sum(row["outcome"] == "correct_diagnosis" for row in expected_cause) / len(expected_cause), 4
            ) if expected_cause else None,
            "false_diagnosis_rate": round(outcomes["wrong_diagnosis"] / len(mode_rows), 4),
            "correct_abstain_rate": round(
                sum(row["outcome"] == "correct_abstain" for row in expected_abstain) / len(expected_abstain), 4
            ) if expected_abstain else None,
            "llm_call_count": sum(row["llm_call_count"] for row in mode_rows),
            "total_tokens": sum(tokens),
            "mean_latency_seconds": round(sum(latencies) / len(latencies), 4) if latencies else None,
            "sources": dict(sorted(Counter(row["source"] for row in mode_rows).items())),
            "violation_codes": dict(sorted(Counter(
                code for row in mode_rows for code in row["violation_codes"]
            ).items())),
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", choices=("scripted", "gemini"), default="scripted")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.client == "gemini":
        client = build_default_client()
        if client is None:
            print("GEMINI_API_KEY is not set; cannot run the gemini client", file=sys.stderr)
            return 2

        def factory(_case: DiagnosisCase) -> DiagnosisLLM | None:
            return client

        model = client.model
    else:
        def factory(case: DiagnosisCase) -> DiagnosisLLM | None:
            return ScriptedDiagnosisLLM(oracle_responder(case), model="scripted-oracle")

        model = "scripted-oracle"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client": args.client,
        "model": model,
        "simulated": args.client == "scripted",
        "evidence": "synthetic fixtures; offline diagnosis-layer evidence only, not live recovery",
        "repeats": args.repeats,
        "accuracy": run_accuracy(client_factory=factory, repeats=max(1, args.repeats)),
        "adversarial": run_adversarial(),
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({
        "accuracy": report["accuracy"]["summary"],
        "adversarial": report["adversarial"]["summary"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
