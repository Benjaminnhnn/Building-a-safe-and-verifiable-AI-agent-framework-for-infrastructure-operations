"""Measure historical staging Verifier evidence without claiming a new live run."""

import hashlib
import json
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "evaluation/aihs/a2-export/manifest.json"
COLLATERAL_STEM = "NET-01-20261108T011025Z"


def read_json(path):
    return json.loads(path.read_text())


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seconds(start, end):
    return (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def measure(root=ROOT):
    manifest = read_json(root / MANIFEST.relative_to(ROOT))
    runs = []
    for source in manifest["source_files"]:
        path = root / source["path"]
        if sha256(path) != source["sha256"]:
            raise ValueError(f"source checksum mismatch: {source['path']}")
        data = read_json(path)
        if data.get("evidence_class") != "operator_approved_staging_drill":
            raise ValueError(f"wrong evidence class: {source['path']}")
        stages = data["stage_times"]
        if data.get("runtime_status", {}).get("status") != "resolved":
            raise ValueError(f"run lacks resolved runtime status: {source['path']}")
        duration = seconds(stages["t_detect"], stages["t_resolved"])
        if duration < 0:
            raise ValueError(f"negative duration: {source['path']}")
        runs.append({"source": source["path"], "scenario": data["scenario_id"], "detect_to_resolved_seconds": duration,
                     "approval_and_planning_seconds": seconds(stages["t_detect"], stages["t_execute_start"]),
                     "execution_seconds": seconds(stages["t_execute_start"], stages["t_execute_end"]),
                     "post_execution_to_resolved_seconds": seconds(stages["t_execute_end"], stages["t_resolved"])})
    durations = [r["detect_to_resolved_seconds"] for r in runs]
    base = root / "terraform/.artifacts/moodle-safe-executor" / COLLATERAL_STEM
    main = read_json(base.with_suffix(".json"))
    companion_path = base.with_name(base.name + "-verifier-collateral.json")
    sidecar_path = Path(str(companion_path) + ".runtime-status.json")
    companion = read_json(companion_path)
    sidecar = read_json(sidecar_path)
    fault = main["controlled_related_fault"]
    verification = companion["verification"]
    if not (fault["fault_source"] == "operator_injected_after_executor"
            and fault["health_only_http_status"] == "200"
            and fault["collateral_damage_demonstrated"] is True
            and fault["resolution_eligible"] is False
            and verification["verdict"] == "collateral_damage"
            and verification["resolution_eligible"] is False
            and verification["simulated"] is False
            and any(p["passed"] is False for p in verification["related_probes"])
            and sidecar["status"] == "verifying"):
        raise ValueError("collateral case no longer supports paired conclusion")
    state = read_json(root / "terraform/terraform.tfstate")
    mapping = read_json(root / "evaluation/aihs/a3-action-mapping.json")
    freeze = read_json(root / "evaluation/aihs/b2-freeze-manifest-2026-11-14.json")
    return {
        "schema_version": 1,
        "measurement_date": "2026-11-14",
        "source_manifest_sha256": sha256(root / MANIFEST.relative_to(ROOT)),
        "historical_staging": {
            "selection": "8 explicitly classified operator-approved staging drill main artifacts; 17 historical main artifacts unclassified",
            "runs": runs,
            "observed_resolved_runs": len(runs),
            "detect_to_resolved_seconds": {"min": min(durations), "median": statistics.median(durations),
                                           "mean": statistics.mean(durations), "max": max(durations)},
            "caveat": "Selected completed runs only; detect-to-RESOLVED includes approval and verification, not service downtime or a population success rate. No AIHS comparison arm.",
        },
        "verifier_paired_case": {
            "source": str(base.with_suffix(".json").relative_to(root)),
            "companion_sha256": sha256(companion_path),
            "runtime_sidecar_sha256": sha256(sidecar_path),
            "controlled_injection_after_executor": True,
            "health_only_http_status": 200,
            "verifier_verdict": verification["verdict"],
            "verifier_resolution_eligible": verification["resolution_eligible"],
            "runtime_status_after_collateral": sidecar["status"],
            "related_failed_probes": [p["name"] for p in verification["related_probes"] if p["passed"] is False],
            "sample_count": len(companion["probe_samples"]),
            "paired_cases": 1,
            "population_false_recovery_rate": None,
            "caveat": "Main run RESOLVED records the normal recovery path; separate collateral sidecar stayed VERIFYING. Do not merge their statuses. Signature presence was not cryptographically verified here.",
        },
        "new_live_staging": {
            "status": "BLOCKED_NOT_RUN",
            "new_runs": 0,
            "local_terraform_resource_count": len(state["resources"]),
            "aws_state_verified": False,
            "aws_check": "target-account CLI requested MFA and could not complete noninteractively in this session",
            "live_reviewed_action_mappings": sum(a.get("status") == "live_reviewed" for a in mapping["actions"]),
            "frozen_test_records": freeze["selected_records"],
            "comparative_rto_seconds": None,
            "service_downtime_seconds": None,
            "rpo_seconds": None,
            "aws_mutations": 0,
        },
    }


if __name__ == "__main__":
    output = ROOT / "evaluation/aihs/b2b3-measurement-2026-11-14.json"
    result = measure()
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"historical_runs": result["historical_staging"]["observed_resolved_runs"],
                      "historical_median_seconds": result["historical_staging"]["detect_to_resolved_seconds"]["median"],
                      "paired_collateral_cases": result["verifier_paired_case"]["paired_cases"],
                      "new_live_status": result["new_live_staging"]["status"]}))
