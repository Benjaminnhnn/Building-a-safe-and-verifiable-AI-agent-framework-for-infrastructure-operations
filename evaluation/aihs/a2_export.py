"""Inventory Moodle evidence and export a minimal, redacted A2 audit dataset.

Only complete top-level staging drill JSON files are parsed for records. This
script deliberately does not convert scenario ground truth or post-decision
evidence into model features. It never exports raw identifiers or free text.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


RUN_FILE = re.compile(r"^((?:CON|DB|NET|RES|SEC)-\d{2})-(\d{8}T\d{6}Z)\.json$")
RUN_PREFIX = re.compile(r"^((?:CON|DB|NET|RES|SEC)-\d{2}-\d{8}T\d{6}Z)(?:\.|-)")
ACTION = re.compile(r"^[A-Za-z_]{1,64}$")
SENSITIVE = re.compile(
    r"-----BEGIN|arn:aws:|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:AKIA|ASIA)[A-Z0-9]{16}"
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def action(value):
    return value if isinstance(value, str) and ACTION.fullmatch(value) else None


def enum_value(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def timestamp(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value) else None


def summarize_inventory(artifact_root, ground_truth_root):
    executor = artifact_root / "moodle-safe-executor"
    files = sorted(p for p in executor.glob("*") if p.is_file()) if executor.exists() else []
    run_ids = set()
    main = []
    for path in files:
        match = RUN_PREFIX.match(path.name)
        if match:
            run_ids.add(match.group(1))
        if RUN_FILE.fullmatch(path.name):
            main.append(path)
    def group(name):
        path = artifact_root / name
        return {"files": sum(p.is_file() for p in path.rglob("*")) if path.exists() else 0,
                "top_level_run_directories": sum(p.is_dir() for p in path.iterdir()) if path.exists() else 0}
    return {
        "moodle_safe_executor": {
            "files": len(files), "distinct_run_prefixes": len(run_ids),
            "top_level_run_json": len(main),
            "run_prefixes_without_top_level_json": len(run_ids - {p.stem for p in main}),
            "run_dates_utc": dict(sorted(Counter(x.rsplit("-", 1)[-1][:8] for x in run_ids).items())),
        },
        "offline_replay": group("aiops-unified-replay"),
        "isolated_lab": group("isolated-live-lab"),
        "historical_sprint2_live": group("moodle-sprint2-live"),
        "ground_truth_fixtures": {
            "moodle": len(list((ground_truth_root / "moodle").glob("*.json"))),
            "erpnext": len(list((ground_truth_root / "erpnext").glob("*.json"))),
            "legacy_top_level": len(list(ground_truth_root.glob("*.json"))),
        },
    }


def make_record(number, group_id, session_id, scenario, source_hash, times,
                proposal_source, proposal_action, gate, approved, executed_action,
                receipt_status, verifier_verdict, runtime_status, outcome):
    reasons = ["missing_predecision_snapshot", "staging_fault_drill_not_natural_incident"]
    if proposal_source != "aihs":
        reasons.append("not_aihs_proposal")
    if outcome != "success":
        reasons.append("no_verified_success_label_for_candidate")
    return {
        "record_id": f"a2-{number:04d}",
        "incident_group_id": group_id,
        "session_group_id": session_id,
        "scenario_code": scenario,
        "provenance": {"evidence_mode": "historical_live_staging",
                       "source_sha256": source_hash,
                       "historical_code_version": "unknown_not_in_artifact"},
        "timeline": {"detected_at": timestamp(times.get("t_detect")),
                     "decision_at": timestamp(times.get("t_plan")),
                     "executed_at": timestamp(times.get("t_execute_start")) if executed_action else None,
                     "verified_at": timestamp(times.get("t_verify")) if verifier_verdict else None},
        "pre_decision": {"snapshot_at": None, "feature_status": "missing_predecision_snapshot", "evidence_ref_hashes": [], "features": {}},
        "proposal": {"source": proposal_source, "action_id": proposal_action, "model_version": None},
        "post_decision": {"gate_decision": gate if gate in ("ALLOW", "DENY", "REQUIRE_APPROVAL") else None,
                          "operator_approved": bool(approved),
                          "executed_action_id": executed_action,
                          "receipt_status": receipt_status,
                          "verifier_verdict": verifier_verdict,
                          "runtime_status": runtime_status},
        "outcome": outcome,
        "training_eligible": False,
        "exclusion_reasons": reasons,
    }


def export(artifact_root, ground_truth_root, output_dir):
    source_dir = artifact_root / "moodle-safe-executor"
    inventory = summarize_inventory(artifact_root, ground_truth_root)
    main_files = sorted(p for p in source_dir.glob("*.json") if RUN_FILE.fullmatch(p.name))
    classifications = Counter()
    sources = []
    records = []
    incident_groups = {}
    sessions = {}
    raw_identifiers = set()
    for path in main_files:
        try:
            obj = json.loads(path.read_text())
        except (UnicodeError, json.JSONDecodeError):
            classifications["invalid_top_level_json"] += 1
            continue
        if not isinstance(obj, dict):
            classifications["invalid_top_level_json"] += 1
            continue
        if obj.get("evidence_class") != "operator_approved_staging_drill":
            classifications["unclassified_historical_main"] += 1
            continue
        match = RUN_FILE.fullmatch(path.name)
        scenario, _ = match.groups()
        times = obj.get("stage_times") or {}
        prepared = obj.get("prepared_request") or {}
        request = prepared.get("request") or {}
        receipt = obj.get("execution_receipt") or {}
        verifier = obj.get("verifier") or {}
        runtime = obj.get("runtime_status") or {}
        if not all((isinstance(times, dict), isinstance(prepared, dict),
                    isinstance(request, dict), isinstance(receipt, dict),
                    isinstance(verifier, dict), isinstance(runtime, dict))):
            classifications["malformed_staging_main"] += 1
            continue
        raw_incident = obj.get("incident_id")
        if not isinstance(raw_incident, str) or not raw_incident:
            classifications["staging_missing_incident_id"] += 1
            continue
        for key in (raw_incident, obj.get("candidate_id"), request.get("request_id"),
                    (obj.get("human_approval") or {}).get("user_id")):
            if isinstance(key, (str, int)) and str(key):
                raw_identifiers.add(str(key))
        group_id = incident_groups.setdefault(raw_incident, f"g{len(incident_groups) + 1:04d}")
        session_id = sessions.setdefault(path.stem, f"s{len(sessions) + 1:04d}")
        source_hash = sha256(path)
        sources.append({"path": str(path), "sha256": source_hash,
                        "evidence_class": "operator_approved_staging_drill",
                        "historical_code_version": "unknown_not_in_artifact"})
        reviewed_action = action(request.get("catalog_action_id"))
        executed_action = action(receipt.get("catalog_action_id")) if receipt.get("status") == "executed" and receipt.get("mutated") is True else None
        gate = (prepared.get("gate_preview") or {}).get("decision")
        approved = (obj.get("human_approval") or {}).get("decision") == "approved"
        receipt_status = enum_value(receipt.get("status"), {"executed", "failed", "blocked", "timeout"})
        verdict = enum_value(verifier.get("verdict"), {"ready_for_runtime_acceptance", "failed", "timeout", "inconclusive"})
        runtime_status = enum_value(runtime.get("status"), {"resolved", "open", "investigating", "failed"})
        if not executed_action:
            outcome = "denied" if gate == "DENY" else "not_executed"
        elif verdict == "ready_for_runtime_acceptance" and verifier.get("resolution_eligible") is True and runtime_status == "resolved":
            outcome = "success"
        elif verdict is None:
            outcome = "missing_evidence"
        elif verdict == "timeout":
            outcome = "timeout"
        else:
            outcome = "failure"
        records.append(make_record(len(records) + 1, group_id, session_id, scenario, source_hash,
                                   times, "reviewed_catalog_plan", reviewed_action, gate,
                                   approved, executed_action, receipt_status, verdict,
                                   runtime_status, outcome))
        shadow = obj.get("ai_shadow_proposal") or {}
        if isinstance(shadow, dict) and shadow.get("proposal"):
            proposal = shadow.get("proposal") or {}
            if not isinstance(proposal, dict):
                proposal = {}
            shadow_gate = shadow.get("gate_decision")
            shadow_outcome = "denied" if shadow_gate == "DENY" else "not_executed"
            records.append(make_record(len(records) + 1, group_id, session_id, scenario,
                                       source_hash, times, "gemini_shadow", action(proposal.get("action_id")),
                                       shadow_gate, False, None, None, None, None, shadow_outcome))
        classifications["explicit_staging_main"] += 1

    # Strict separation: the only future model input is pre_decision.features.
    for record in records:
        features = record["pre_decision"]["features"]
        forbidden = ("scenario", "ground_truth", "outcome", "verifier", "gate",
                     "approval", "receipt", "action", "incident_id", "run_id")
        if any(any(token in key.lower() for token in forbidden) for key in features):
            raise ValueError("post-decision or answer-bearing feature name")
        if record["training_eligible"] and not features:
            raise ValueError("training row without pre-decision features")
    serialized = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    if SENSITIVE.search(serialized) or any(x in serialized for x in raw_identifiers if len(x) >= 6):
        raise ValueError("sensitive content or raw identifier leaked into export")
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "audit-records.jsonl"
    audit_path.write_text(serialized)
    manifest = {
        "schema_version": 1,
        "source_policy": "only top-level main JSON with explicit operator_approved_staging_drill class",
        "inventory": inventory,
        "classifications": dict(sorted(classifications.items())),
        "source_files": sources,
        "distinct_incidents": len(incident_groups),
        "candidate_records": len(records),
        "by_proposal_source": dict(sorted(Counter(r["proposal"]["source"] for r in records).items())),
        "by_scenario": dict(sorted(Counter(r["scenario_code"] for r in records).items())),
        "by_action": dict(sorted(Counter(r["proposal"]["action_id"] or "unknown" for r in records).items())),
        "by_outcome": dict(sorted(Counter(r["outcome"] for r in records).items())),
        "training_eligible": sum(r["training_eligible"] for r in records),
        "leakage_scan": "passed_no_raw_identifiers_or_sensitive_patterns",
        "audit_records_sha256": sha256(audit_path),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=Path("terraform/.artifacts"))
    parser.add_argument("--ground-truth-root", type=Path, default=Path("evaluation/ground_truth"))
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation/aihs/a2-export"))
    args = parser.parse_args()
    result = export(args.artifact_root, args.ground_truth_root, args.output_dir)
    print(json.dumps({k: result[k] for k in ("distinct_incidents", "candidate_records", "by_proposal_source", "by_scenario", "by_outcome", "training_eligible", "leakage_scan")}, ensure_ascii=False))
