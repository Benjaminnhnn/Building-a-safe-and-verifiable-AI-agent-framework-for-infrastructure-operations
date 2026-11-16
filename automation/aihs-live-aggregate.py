#!/usr/bin/env python3
"""Aggregate the AIHS main live campaign: C3 (Verifier) vs C2 derived from each run's health_only_view.

    python automation/aihs-live-aggregate.py OUT_DIR [--json results.json] [--md results.md]

Read-only over run.json files. C2 is the paired health-only view of the same C3 execution (plan section 1, option b).
Ground truth is used here only for post-run scoring.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

_spec = importlib.util.spec_from_file_location("aihs_live_summary", Path(__file__).with_name("aihs-live-summary.py"))
_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_summary)

RTO_KEYS = ("inject_to_detect_s", "detect_to_proposal_s", "approval_wait_s", "execute_s", "detect_to_health_claim_s",
            "detect_to_true_recovery_s", "inject_to_true_recovery_s", "verify_s", "detect_to_resolved_s")


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def _rate(num, den):
    return {"n": num, "of": den, "rate": round(num / den, 3) if den else None}


def aggregate(records: list[dict]) -> dict:
    by_scenario = defaultdict(list)
    for rec in records:
        by_scenario[(rec.get("scoring") or {}).get("scenario_ground_truth")].append(rec)

    def block(recs):
        executed = [r for r in recs if (r.get("execution") or {}) and r.get("outcome") not in (
            "OPERATOR_REJECTED", "OPERATOR_TIMEOUT", "HUMAN_HANDOFF", "OBSERVE_ONLY_NO_ACTION", "HANDOFF_BLOCKED")]
        c2_claims = [r for r in executed if (r.get("health_only_view") or {}).get("claimed_recovered")]
        c3_resolved = [r for r in recs if r.get("outcome") == "RESOLVED_BY_VERIFIER"]
        true_recovered = [r for r in executed if (r.get("scoring") or {}).get("recovered_true")]
        proposals = [r for r in recs if _summary.ACTION_SCENARIO.get((r.get("decision") or {}).get("proposed_action"))]
        correct_proposals = [r for r in proposals if _summary.ACTION_SCENARIO[(r["decision"]["proposed_action"])]
                             == r["scoring"]["scenario_ground_truth"]]
        no_wait = [m["detect_to_true_recovery_s"] - m["approval_wait_s"] for m in (r.get("metrics") or {} for r in true_recovered)
                   if m.get("detect_to_true_recovery_s") is not None and m.get("approval_wait_s") is not None]
        return {
            "runs": len(recs),
            "outcomes": dict(Counter(r.get("outcome") for r in recs)),
            "operator": dict(Counter((r.get("operator") or {}).get("decision") or "none" for r in recs)),
            "model_proposed_repair_action": len(proposals),
            "model_proposed_correct_action": _rate(len(correct_proposals), len(proposals)),
            "executed": len(executed),
            "true_recovered_of_executed": _rate(len(true_recovered), len(executed)),
            "C2_health_only_claims": len(c2_claims),
            "C2_false_recovery": _rate(sum(bool((r.get("scoring") or {}).get("false_recovery_health_only")) for r in c2_claims), len(c2_claims)),
            "C3_resolved": _rate(len(c3_resolved), len(recs)),
            "C3_false_resolved": _rate(sum(bool((r.get("scoring") or {}).get("false_resolved")) for r in c3_resolved), len(c3_resolved)),
            "C3_missed_true_recovery": sum(1 for r in true_recovered if r.get("outcome") != "RESOLVED_BY_VERIFIER"),
            "invariants_all_held": all(all((r.get("invariants") or {}).values()) for r in recs),
            "resets_verified": _rate(sum(r.get("reset_verified") is True for r in recs), len(recs)),
            "median_s": {k: _median([(r.get("metrics") or {}).get(k) for r in true_recovered]) for k in RTO_KEYS},
            "median_detect_to_true_recovery_minus_approval_s": _median(no_wait),
            "rpo": [{"seq": r.get("seq"), **(r.get("rpo") or {})} for r in recs if (r.get("rpo") or {}).get("defined")],
        }

    return {
        "runs": [{"seq": r.get("seq"), "run_id": r.get("run_id"), "scenario": r["scoring"]["scenario_ground_truth"],
                  "proposed_action": (r.get("decision") or {}).get("proposed_action"),
                  "gate_reason": (r.get("decision") or {}).get("reason"),
                  "operator": (r.get("operator") or {}).get("decision"), "outcome": r.get("outcome"),
                  "C2_claimed": (r.get("health_only_view") or {}).get("claimed_recovered"),
                  "recovered_true": (r.get("scoring") or {}).get("recovered_true"),
                  "detect_to_true_recovery_s": (r.get("metrics") or {}).get("detect_to_true_recovery_s")} for r in records],
        "overall": block(records),
        "by_scenario": {k: block(v) for k, v in sorted(by_scenario.items(), key=lambda kv: str(kv[0]))},
    }


def markdown(result: dict) -> str:
    def r(x):
        return "-" if x is None or x.get("of") == 0 else f"{x['n']}/{x['of']}"
    lines = ["| Kịch bản | Lượt | Đề xuất đúng | Thực thi | Hồi phục thật | C2 tuyên bố (sai) | C3 RESOLVED (sai) | Trung vị detect→hồi phục thật (bỏ chờ duyệt) |",
             "|---|---|---|---|---|---|---|---|"]
    for name, b in list(result["by_scenario"].items()) + [("**Tổng**", result["overall"])]:
        med, nowait = b["median_s"]["detect_to_true_recovery_s"], b["median_detect_to_true_recovery_minus_approval_s"]
        lines.append(f"| {name} | {b['runs']} | {r(b['model_proposed_correct_action'])} | {b['executed']} | "
                     f"{r(b['true_recovered_of_executed'])} | {b['C2_health_only_claims']} ({b['C2_false_recovery']['n']}) | "
                     f"{b['C3_resolved']['n']} ({b['C3_false_resolved']['n']}) | "
                     f"{'-' if med is None else f'{med} s'}{'' if nowait is None else f' ({nowait} s)'} |")
    o = result["overall"]
    lines += ["", f"Kết quả theo lượt: {o['outcomes']}. Operator: {o['operator']}.",
              f"Bất biến an toàn đều đạt: {o['invariants_all_held']}; reset đã kiểm tra: {r(o['resets_verified'])};"
              f" C3 bỏ sót hồi phục thật: {o['C3_missed_true_recovery']}."]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--md", type=Path)
    args = parser.parse_args()
    records = [rec for rec in _summary.runs(args.out_dir) if rec.get("scoring")]
    result = aggregate(records)
    text = markdown(result)
    print(text)
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if args.md:
        args.md.write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
