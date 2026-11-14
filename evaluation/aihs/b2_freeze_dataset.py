"""Freeze a leakage-checked AIHS evaluation cohort, or fail closed.

Feature and label files are separate. The held-out test labels remain in the
Git-ignored output directory; no model is fit and no ground-truth fixture is
read by this script. A blocked run writes only a manifest, never split files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from a2_split import eligible, grouped_chronological_split, parse_time


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation/aihs"
FORBIDDEN_FEATURE = re.compile(
    r"scenario|ground.?truth|outcome|verifier|gate|approval|receipt|"
    r"action|incident|run.?id|fault.?tag|answer|label|post.?decision",
    re.IGNORECASE,
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    def reject_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")
    return [json.loads(line, parse_constant=reject_constant)
            for line in path.read_text().splitlines() if line]


def check_features(row: dict) -> None:
    for name, value in row["pre_decision"]["features"].items():
        if FORBIDDEN_FEATURE.search(name):
            raise ValueError(f"answer-bearing feature name in {row['record_id']}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"non-finite feature in {row['record_id']}")


def review_index(rows: list[dict], path: Path | None) -> dict[str, dict]:
    if path is None or not path.is_file():
        return {}
    known = {row["record_id"]: row for row in rows}
    result = {}
    for review in read_jsonl(path):
        if set(review) != {"record_id", "outcome", "executed_action_id", "reviewer_id",
                           "evidence_sha256", "reviewed_at"}:
            raise ValueError("label review has missing or extra fields")
        record_id = review["record_id"]
        if record_id not in known or record_id in result:
            raise ValueError("label review has unknown or duplicate record ID")
        row = known[record_id]
        if (review["outcome"] not in {"success", "failure"}
                or review["outcome"] != row["outcome"]
                or review["executed_action_id"] != row["post_decision"]["executed_action_id"]
                or not isinstance(review["reviewer_id"], str) or not review["reviewer_id"].strip()
                or not isinstance(review["evidence_sha256"], str)
                or not SHA256.fullmatch(review["evidence_sha256"])):
            raise ValueError("label review does not independently bind the observed outcome")
        reviewed_at = parse_time(review["reviewed_at"])
        decision_at = parse_time(row["timeline"]["decision_at"])
        verified_at = parse_time(row["timeline"]["verified_at"]) if row["timeline"]["verified_at"] else None
        if (reviewed_at.tzinfo is None or reviewed_at <= decision_at
                or verified_at is None or reviewed_at <= verified_at):
            raise ValueError("label review must occur after independent verification")
        result[record_id] = review
    return result


def freeze(*, records: Path, source_manifest: Path, schema_path: Path,
           label_review: Path | None, output_root: Path, manifest_out: Path,
           allowed_mode: str = "historical_live_staging", min_groups: int = 10) -> dict:
    source = json.loads(source_manifest.read_text())
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    if digest(records) != source["audit_records_sha256"]:
        raise ValueError("A2 audit checksum differs from source manifest")
    for entry in source["source_files"]:
        path = ROOT / entry["path"]
        if not path.is_file() or digest(path) != entry["sha256"]:
            raise ValueError("A2 source artifact checksum differs from manifest")
    rows = read_jsonl(records)
    if len(rows) != source["candidate_records"]:
        raise ValueError("A2 row count differs from source manifest")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    seen = set()
    for row in rows:
        validator.validate(row)
        if row["record_id"] in seen:
            raise ValueError("duplicate audit record ID")
        seen.add(row["record_id"])
        check_features(row)

    reviews = review_index(rows, label_review)
    selected = []
    excluded = Counter()
    for row in rows:
        row_reasons = []
        if row["provenance"]["evidence_mode"] != allowed_mode:
            row_reasons.append("different_evidence_mode")
        if row["provenance"]["historical_code_version"] in {"", "unknown_not_in_artifact"}:
            row_reasons.append("unknown_historical_code_version")
        if row["exclusion_reasons"] or not eligible(row) or row["post_decision"]["receipt_status"] != "executed" \
                or not row["timeline"]["verified_at"]:
            row_reasons.append("missing_verified_predecision_features_or_observed_label")
        if row["outcome"] in {"success", "failure"} and row["record_id"] not in reviews:
            row_reasons.append("missing_independent_label_review")
        if row_reasons:
            excluded.update(row_reasons)
        else:
            selected.append(row)

    groups = grouped_chronological_split(selected)
    reasons = []
    if not selected:
        reasons.append("zero_eligible_independently_labeled_records")
    if len(groups) < min_groups:
        reasons.append(f"fewer_than_{min_groups}_independent_incident_session_groups")
    if selected and len({row["outcome"] for row in selected}) < 2:
        reasons.append("only_one_observed_outcome_class")
    n = len(groups)
    train_end, valid_end = int(n * 0.70), int(n * 0.85)
    assigned_groups = {
        "train": groups[:train_end],
        "validation": groups[train_end:valid_end],
        "test": groups[valid_end:],
    }
    assigned = {name: [row for group in blocks for row in group]
                for name, blocks in assigned_groups.items()}
    if selected and any(not assigned[name] for name in assigned):
        reasons.append("empty_split")
    if all(assigned.values()):
        for name, subset in assigned.items():
            if len({row["outcome"] for row in subset}) < 2:
                reasons.append(f"{name}_split_has_only_one_outcome_class")
        maximum_train = max(parse_time(r["timeline"]["decision_at"]) for r in assigned["train"])
        minimum_validation = min(parse_time(r["timeline"]["decision_at"]) for r in assigned["validation"])
        maximum_validation = max(parse_time(r["timeline"]["decision_at"]) for r in assigned["validation"])
        minimum_test = min(parse_time(r["timeline"]["decision_at"]) for r in assigned["test"])
        if not maximum_train < minimum_validation or not maximum_validation < minimum_test:
            reasons.append("incident_group_spans_cross_temporal_boundaries")

    label_hash = digest(label_review) if label_review is not None and label_review.is_file() else None
    identity = {
        "audit_sha256": digest(records), "schema_sha256": digest(schema_path),
        "source_manifest_sha256": digest(source_manifest),
        "freeze_code_sha256": digest(Path(__file__)),
        "split_code_sha256": digest(EVAL / "a2_split.py"),
        "label_review_sha256": label_hash, "allowed_mode": allowed_mode,
        "min_groups": min_groups, "split_policy": "chronological_70_15_15_connected_incident_session",
    }
    freeze_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    manifest = {
        "schema_version": 1, "stage": "B2.1_evaluation_freeze",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED" if reasons else "READY",
        "freeze_id": freeze_id, "identity": identity,
        "total_audit_records": len(rows), "selected_records": len(selected),
        "independent_groups": len(groups),
        "by_scenario": dict(sorted(Counter(row["scenario_code"] for row in selected).items())),
        "by_action": dict(sorted(Counter(row["post_decision"]["executed_action_id"] for row in selected).items())),
        "by_outcome": dict(sorted(Counter(row["outcome"] for row in selected).items())),
        "all_record_proposal_sources": dict(sorted(Counter(row["proposal"]["source"] for row in rows).items())),
        "all_record_by_scenario": dict(sorted(Counter(row["scenario_code"] for row in rows).items())),
        "all_record_by_action": dict(sorted(Counter(row["proposal"]["action_id"] or "unknown" for row in rows).items())),
        "all_record_by_outcome": dict(sorted(Counter(row["outcome"] for row in rows).items())),
        "excluded": dict(sorted(excluded.items())), "reasons": reasons,
        "model_input_policy": "only_pre_decision_features_no_scenario_or_post_decision_fields",
        "lda_fit_policy": "fit_train_only_transform_validation_and_test",
        "test_policy": "held_out_no_tuning_or_refit_after_freeze",
        "test_labels_storage": "git_ignored_local_output_only",
        "splits": None,
    }
    if not reasons:
        frozen = output_root / freeze_id
        if frozen.exists():
            raise ValueError("freeze ID already exists; immutable output cannot be overwritten")
        frozen.mkdir(parents=True)
        summary = {}
        for name, group_list in assigned_groups.items():
            subset = assigned[name]
            features = [json.dumps({"record_id": row["record_id"],
                                    "features": row["pre_decision"]["features"]}, sort_keys=True) + "\n"
                        for row in subset]
            labels = [json.dumps({"record_id": row["record_id"], "outcome": row["outcome"],
                                  "executed_action_id": row["post_decision"]["executed_action_id"]}, sort_keys=True) + "\n"
                      for row in subset]
            paths = {"features": frozen / f"{name}-features.jsonl",
                     "labels": frozen / f"{name}-labels.jsonl"}
            paths["features"].write_text("".join(features))
            paths["labels"].write_text("".join(labels))
            summary[name] = {
                "groups": len(group_list), "records": len(subset),
                "outcomes": dict(sorted(Counter(row["outcome"] for row in subset).items())),
                "evidence_modes": dict(sorted(Counter(row["provenance"]["evidence_mode"] for row in subset).items())),
                "historical_code_versions": dict(sorted(Counter(row["provenance"]["historical_code_version"] for row in subset).items())),
                "features_sha256": digest(paths["features"]),
                "labels_sha256": digest(paths["labels"]),
            }
        manifest["splits"] = summary
        manifest["frozen_data_local"] = str(frozen.relative_to(ROOT)) if frozen.is_relative_to(ROOT) else str(frozen)

    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, default=EVAL / "a2-export/audit-records.jsonl")
    parser.add_argument("--source-manifest", type=Path, default=EVAL / "a2-export/manifest.json")
    parser.add_argument("--schema", type=Path, default=EVAL / "a2-record.schema.json")
    parser.add_argument("--label-review", type=Path)
    parser.add_argument("--output-root", type=Path, default=EVAL / ".artifacts/b2-freeze")
    parser.add_argument("--manifest-out", type=Path, default=EVAL / "b2-freeze-manifest-2026-11-14.json")
    parser.add_argument("--allowed-mode", choices=("historical_live_staging", "lab", "offline_replay", "synthetic"),
                        default="historical_live_staging")
    parser.add_argument("--min-groups", type=int, default=10)
    args = parser.parse_args()
    if args.min_groups < 3:
        parser.error("min-groups must be at least 3")
    result = freeze(records=args.records, source_manifest=args.source_manifest,
                    schema_path=args.schema, label_review=args.label_review,
                    output_root=args.output_root, manifest_out=args.manifest_out,
                    allowed_mode=args.allowed_mode, min_groups=args.min_groups)
    print(json.dumps({key: result[key] for key in
                      ("status", "freeze_id", "total_audit_records", "selected_records",
                       "independent_groups", "excluded", "reasons", "splits")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
