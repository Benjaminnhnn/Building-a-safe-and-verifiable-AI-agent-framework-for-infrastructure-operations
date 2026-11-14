"""Re-run the pinned public AIHS prototype and validate the A1-A3 handoff.

Uses only the local, checksum-verified source. Never reads AWS credentials,
Terraform state, or production telemetry. Generated models stay Git-ignored.
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation/aihs"
PACKAGE_FILES = (
    "evaluation/aihs/source-manifest.json",
    "evaluation/aihs/requirements-a1.txt",
    "evaluation/aihs/requirements-a4.txt",
    "evaluation/aihs/baseline-prototype.json",
    "evaluation/aihs/reproduction-results-2026-11-14.json",
    "evaluation/aihs/check_controller_fallback.py",
    "evaluation/aihs/compare_retrained.py",
    "evaluation/aihs/a2-record.schema.json",
    "evaluation/aihs/a2_export.py",
    "evaluation/aihs/a2_split.py",
    "evaluation/aihs/a2-export/audit-records.jsonl",
    "evaluation/aihs/a2-export/manifest.json",
    "evaluation/aihs/a2-export/split-manifest.json",
    "evaluation/aihs/a3-action-mapping.json",
    "evaluation/aihs/a3-examples.json",
    "evaluation/aihs/a4_acceptance.py",
    "agent_src/core/moodle/aihs_contract.py",
    "agent_src/tests/test_aihs_contract.py",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(command, *, cwd=ROOT, timeout=900, env=None):
    completed = subprocess.run(
        [str(part) for part in command], cwd=cwd, env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed (exit {completed.returncode}): {command}; "
            f"stderr tail: {completed.stderr[-500:]}"
        )
    return completed


def validate_bundle(source):
    manifest = json.loads((EVAL / "source-manifest.json").read_text())
    baseline = json.loads((EVAL / "baseline-prototype.json").read_text())
    reproduced = json.loads((EVAL / "reproduction-results-2026-11-14.json").read_text())
    commit = manifest["source"]["commit"]
    require(commit == baseline["source"]["commit"] == reproduced["source_commit"], "source commit mismatch")
    archive = ROOT / manifest["source"]["archive"]["path"]
    require(archive.is_file() and digest(archive) == manifest["source"]["archive"]["sha256"], "archive checksum mismatch")
    source_root = source.parent
    for entry in manifest["source"]["files"]:
        path = source_root / entry["path"]
        require(path.is_file() and digest(path) == entry["sha256"], f"source checksum mismatch: {entry['path']}")
    paper = ROOT / manifest["paper"]["file"]["path"]
    require(paper.is_file() and digest(paper) == manifest["paper"]["file"]["sha256"], "paper checksum mismatch")
    return commit, manifest


def validate_a2(work):
    exported = EVAL / "a2-export"
    audit = exported / "audit-records.jsonl"
    manifest = json.loads((exported / "manifest.json").read_text())
    require(digest(audit) == manifest["audit_records_sha256"], "A2 audit checksum mismatch")
    schema = json.loads((EVAL / "a2-record.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    rows = [json.loads(line) for line in audit.read_text().splitlines() if line]
    for row in rows:
        validator.validate(row)
    require(len(rows) == manifest["candidate_records"], "A2 row count mismatch")
    require(sum(row["training_eligible"] for row in rows) == manifest["training_eligible"], "A2 eligibility mismatch")
    split_dir = work / "split"
    split_result = run([
        sys.executable, EVAL / "a2_split.py", "--records", audit,
        "--output-dir", split_dir,
    ])
    observed = json.loads(split_result.stdout)
    expected = json.loads((exported / "split-manifest.json").read_text())
    require(observed == expected, "A2 split manifest mismatch")
    require(observed["status"] == "blocked_insufficient_data", "unexpected A2 split status")
    require(not any(split_dir.glob("*-ids.txt")), "unexpected A2 split files")
    return {"schema_valid_records": len(rows), "audit_sha256": digest(audit),
            "eligible_records": observed["eligible_records"], "split_status": observed["status"]}


def validate_a3():
    mapping = json.loads((EVAL / "a3-action-mapping.json").read_text())
    actions = mapping["actions"]
    require(mapping["review_status"] == "no_approved_action_mapping", "unexpected A3 review status")
    require({entry["aihs_action"] for entry in actions} == {"NOP", "MSR", "ER", "RB", "RI", "RMA"}, "A3 action set mismatch")
    require(len(actions) == 6 and all(entry["catalog_action_id"] is None for entry in actions), "A3 mapping is not closed")
    require(all(entry["status"] in {"unmapped", "human_only"} for entry in actions), "unexpected A3 action status")
    examples = json.loads((EVAL / "a3-examples.json").read_text())["examples"]
    require(all(example["adapter_result"]["execution_permitted"] is False for example in examples), "executable A3 example")
    require(all(example["adapter_result"]["dispatched"] is False for example in examples), "dispatched A3 example")
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "agent_src"))
    tested = run([
        sys.executable, "-m", "unittest", "discover", "-s", "agent_src/tests",
        "-p", "test_aihs_contract.py", "-v",
    ], env=environment)
    require("Ran 7 tests" in tested.stderr and "OK" in tested.stderr, "A3 contract tests incomplete")
    return {"mapping_review_status": mapping["review_status"],
            "approved_actions": 0, "contract_tests_passed": 7}


def rerun_prototype(source, python, work, manifest):
    copy = work / "AIHS_prototype"
    shutil.copytree(source, copy)
    pretrained = run([python, "AIHS_api.py"], cwd=copy, timeout=90)
    sample = ast.literal_eval(pretrained.stdout.strip())
    require(sample["action"] == ["RI"], "pretrained sample differs")
    run([python, "DataGenerator.py"], cwd=copy, timeout=90)
    csv = copy / "dataset/train.csv"
    require(digest(csv) == next(
        entry["sha256"] for entry in manifest["source"]["files"]
        if entry["path"] == "AIHS_prototype/dataset/train.csv"
    ), "generated CSV checksum mismatch")
    training = run([python, "AIHS_Training.py"], cwd=copy, timeout=900)
    require("training completed" in training.stdout.lower(), "training completion marker missing")
    trained = run([python, "AIHS_api.py"], cwd=copy, timeout=90)
    trained_sample = ast.literal_eval(trained.stdout.strip())
    require(trained_sample["action"] == ["RI"], "trained sample differs")
    fallback = run([python, EVAL / "check_controller_fallback.py", source], timeout=90)
    fallback_result = json.loads(fallback.stdout)
    require(fallback_result["observed_upstream_output"] == "call human", "upstream fallback differs")
    return {
        "pretrained_sample_action": sample["action"],
        "generated_csv_sha256": digest(csv),
        "training_exit": 0,
        "trained_sample_action": trained_sample["action"],
        "upstream_fallback": fallback_result["observed_upstream_output"],
        "model_sha256": {path.name: digest(path) for path in sorted((copy / "model").glob("*.pkl"))},
        "validation_scope": "bundled_synthetic_data_only",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=EVAL / ".artifacts/dcbrain-07314287ca0858b9a7d8dba951c96394487e2be2/AIHS_prototype")
    parser.add_argument("--python", type=Path, default=EVAL / ".artifacts/venv/bin/python")
    parser.add_argument("--output", type=Path, default=EVAL / "a4-acceptance-2026-11-14.json")
    args = parser.parse_args()
    source = args.source.resolve()
    # Keep the venv symlink path: resolving it selects the system interpreter.
    python = args.python.absolute()
    require(source.is_dir() and python.is_file(), "source or Python environment missing")
    with tempfile.TemporaryDirectory(prefix="a4-", dir=EVAL / ".artifacts") as temp:
        work = Path(temp)
        commit, manifest = validate_bundle(source)
        a2 = validate_a2(work)
        a3 = validate_a3()
        prototype = rerun_prototype(source, python, work, manifest)
    result = {
        "schema_version": 1,
        "stage": "A4_handoff_acceptance",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": commit,
        "package_sha256": {path: digest(ROOT / path) for path in PACKAGE_FILES},
        "source_checksums": "passed",
        "a2": a2,
        "a3": a3,
        "prototype_rerun": prototype,
        "handoff_status": "accepted_with_research_gaps",
        "open_gaps": ["no_moodle_training_rows", "no_reviewed_aihs_action_mapping", "no_live_gate_or_recovery_evidence"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: result[key] for key in ("source_commit", "a2", "a3", "prototype_rerun", "handoff_status")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
