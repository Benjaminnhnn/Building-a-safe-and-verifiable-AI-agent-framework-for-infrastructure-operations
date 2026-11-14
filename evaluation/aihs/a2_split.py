"""Chronological, incident/session-grouped split for eligible A2 records.

If the input is insufficient, write a blocked manifest and no split files.
This script never fits LDA or reads ground-truth fixtures.
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def eligible(row):
    pre = row["pre_decision"]
    post = row["post_decision"]
    timeline = row["timeline"]
    if not row["training_eligible"] or row["outcome"] not in ("success", "failure"):
        return False
    if pre["feature_status"] != "verified_predecision" or not pre["features"] or not pre["evidence_ref_hashes"]:
        return False
    if not pre["snapshot_at"] or not timeline["decision_at"]:
        return False
    if parse_time(pre["snapshot_at"]) > parse_time(timeline["decision_at"]):
        return False
    if not post["executed_action_id"] or post["executed_action_id"] != row["proposal"]["action_id"]:
        return False
    return True


def grouped_chronological_split(rows):
    """Join rows sharing an incident or session, then order groups by time."""
    parent = list(range(len(rows)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def union(left, right):
        parent[find(left)] = find(right)
    first_incident, first_session = {}, {}
    for index, row in enumerate(rows):
        for table, key in ((first_incident, row["incident_group_id"]),
                           (first_session, row["session_group_id"])):
            if key in table:
                union(index, table[key])
            else:
                table[key] = index
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[find(index)].append(row)
    ordered = sorted(groups.values(), key=lambda group: min(parse_time(r["timeline"]["decision_at"]) for r in group))
    return ordered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, default=Path("evaluation/aihs/a2-export/audit-records.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation/aihs/a2-export"))
    parser.add_argument("--min-groups", type=int, default=10)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.records.read_text().splitlines() if line]
    selected = [row for row in rows if eligible(row)]
    groups = grouped_chronological_split(selected)
    reasons = []
    if not selected:
        reasons.append("zero_rows_with_verified_predecision_features_and_observed_action_labels")
    if len(groups) < args.min_groups:
        reasons.append(f"fewer_than_{args.min_groups}_independent_incident_session_groups")
    if selected and len({row["outcome"] for row in selected}) < 2:
        reasons.append("only_one_observed_outcome_class")
    result = {
        "schema_version": 1,
        "strategy": "chronological_70_15_15_by_connected_incident_session_groups",
        "train_lda_fit_policy": "train_only_never_validation_or_test",
        "total_audit_records": len(rows),
        "eligible_records": len(selected),
        "independent_groups": len(groups),
        "observed_outcomes": dict(sorted(Counter(row["outcome"] for row in selected).items())),
        "status": "blocked_insufficient_data" if reasons else "ready",
        "reasons": reasons,
        "splits": None,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not reasons:
        n = len(groups)
        train_end = int(n * 0.70)
        valid_end = int(n * 0.85)
        assigned = {"train": groups[:train_end], "validation": groups[train_end:valid_end], "test": groups[valid_end:]}
        result["splits"] = {name: {"groups": len(value), "records": sum(map(len, value))} for name, value in assigned.items()}
        for name, group_list in assigned.items():
            with (args.output_dir / f"{name}-ids.txt").open("w") as handle:
                for group in group_list:
                    for row in group:
                        handle.write(row["record_id"] + "\n")
    else:
        for name in ("train", "validation", "test"):
            (args.output_dir / f"{name}-ids.txt").unlink(missing_ok=True)
    (args.output_dir / "split-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
