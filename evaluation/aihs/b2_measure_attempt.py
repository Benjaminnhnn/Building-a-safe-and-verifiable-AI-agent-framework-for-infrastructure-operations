"""Evidence-bound B2.2 measurement attempt; unavailable metrics stay null.

Historical Moodle drill decisions and synthetic shadow fixtures are reported
as separate observations. Neither may become an empirical AIHS denominator.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation/aihs"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure(*, freeze_path: Path, audit_path: Path, audit_manifest_path: Path,
            mapping_path: Path, shadow_path: Path, output_path: Path) -> dict:
    freeze = json.loads(freeze_path.read_text())
    source = json.loads(audit_manifest_path.read_text())
    mapping = json.loads(mapping_path.read_text())
    shadow = json.loads(shadow_path.read_text())
    if sha256(audit_path) != source["audit_records_sha256"]:
        raise ValueError("A2 audit checksum mismatch")
    if freeze["identity"]["audit_sha256"] != sha256(audit_path):
        raise ValueError("freeze input differs from A2 audit")
    rows = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    if len(rows) != source["candidate_records"] or len(rows) != freeze["total_audit_records"]:
        raise ValueError("record count differs between measurement inputs")
    historical = {}
    for proposal_source in sorted({row["proposal"]["source"] for row in rows}):
        subset = [row for row in rows if row["proposal"]["source"] == proposal_source]
        historical[proposal_source] = {
            "records": len(subset),
            "gate_decisions": dict(sorted(Counter(row["post_decision"]["gate_decision"] or "NO_DECISION"
                                                  for row in subset).items())),
            "outcomes": dict(sorted(Counter(row["outcome"] for row in subset).items())),
        }
    synthetic_cases = [case for case in shadow["cases"] if case["input_class"] == "synthetic_test_fixture"]
    prototype_cases = [case for case in shadow["cases"] if case["input_class"] == "public_prototype_synthetic"]
    approved_mapping = sum(entry["status"] in {"shadow_reviewed", "live_reviewed"}
                           for entry in mapping["actions"])
    reasons = []
    if freeze["status"] != "READY" or not freeze.get("splits"):
        reasons.append("no_frozen_independent_test_set")
    if approved_mapping == 0:
        reasons.append("no_reviewed_aihs_to_moodle_action_mapping")
    if not any(row["proposal"]["source"] == "aihs" for row in rows):
        reasons.append("no_moodle_aihs_proposals")
    # The A2 outcome is an observed action result, not an independent label of
    # the best action, and Gate safety cannot be inferred from recovery alone.
    reasons.extend(("no_independent_acceptable_action_labels",
                    "no_blind_predecision_proposal_safety_labels"))
    result = {
        "schema_version": 1,
        "stage": "B2.2_model_and_safety_gate_measurement_attempt",
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "NOT_MEASURABLE",
        "input_sha256": {
            "freeze_manifest": sha256(freeze_path),
            "a2_audit": sha256(audit_path),
            "a2_manifest": sha256(audit_manifest_path),
            "mapping": sha256(mapping_path),
            "shadow_replay": sha256(shadow_path),
        },
        "independent_test_records": freeze["splits"]["test"]["records"] if freeze.get("splits") else 0,
        "aihs_moodle_proposals": sum(row["proposal"]["source"] == "aihs" for row in rows),
        "reviewed_action_mappings": approved_mapping,
        "model_metrics": {
            "status": "NOT_COMPUTED",
            "top1_all": None, "topk": None, "macro_f1": None,
            "abstention_rate": None, "hit_rate": None,
            "reverse_rate": None, "overall_rate": None,
            "scored_test_records": 0,
        },
        "safety_gate_metrics": {
            "status": "NOT_COMPUTED",
            "unsafe_denied": None, "unsafe_labeled": 0,
            "safe_denied": None, "safe_labeled": 0,
            "unsafe_block_rate": None, "false_block_rate": None,
            "require_approval_is_separate_from_deny": True,
            "counterfactual_no_gate_dispatched": False,
        },
        "descriptive_only": {
            "historical_non_aihs_audit": historical,
            "prototype_on_bundled_synthetic_cases": len(prototype_cases),
            "synthetic_shadow_fixture_cases": len(synthetic_cases),
            "synthetic_fixture_gate_decisions": dict(sorted(Counter(
                case["gate_decision"] or "NOT_EVALUATED" for case in synthetic_cases
            ).items())),
            "shadow_executor_calls": shadow["executor_calls"],
        },
        "reasons": reasons,
        "required_for_model_scoring": [
            "frozen_test_with_verified_predecision_features_and_model_predictions",
            "blind_independent_acceptable_action_labels_for_each_test_incident",
            "reviewed_aihs_action_mapping_and_fixed_metric_definitions",
        ],
        "required_for_gate_scoring": [
            "same_frozen_proposals_replayed_through_actual_gate_without_dispatch",
            "blind_predecision_safe_or_unsafe_adjudication_for_each_proposal",
            "proposal_matched_gate_audit_with_allow_deny_require_approval_kept_separate",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", type=Path, default=EVAL / "b2-freeze-manifest-2026-11-14.json")
    parser.add_argument("--audit", type=Path, default=EVAL / "a2-export/audit-records.jsonl")
    parser.add_argument("--audit-manifest", type=Path, default=EVAL / "a2-export/manifest.json")
    parser.add_argument("--mapping", type=Path, default=EVAL / "a3-action-mapping.json")
    parser.add_argument("--shadow", type=Path, default=EVAL / "b1-shadow-replay-2026-11-14.json")
    parser.add_argument("--output", type=Path, default=EVAL / "b2-measurement-2026-11-14.json")
    args = parser.parse_args()
    result = measure(freeze_path=args.freeze, audit_path=args.audit,
                     audit_manifest_path=args.audit_manifest, mapping_path=args.mapping,
                     shadow_path=args.shadow, output_path=args.output)
    print(json.dumps({key: result[key] for key in
                      ("status", "independent_test_records", "aihs_moodle_proposals",
                       "reviewed_action_mappings", "model_metrics", "safety_gate_metrics",
                       "descriptive_only", "reasons")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
