"""A2 exporter lab path, schema validity, redaction, checksums and the empty independent-label form."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "evaluation/aihs") not in sys.path:
    sys.path.insert(0, str(ROOT / "evaluation/aihs"))

import a2_export  # noqa: E402
import a2_export_lab as lab  # noqa: E402
import lab_label_form  # noqa: E402
from b2_freeze_dataset import FORBIDDEN_FEATURE  # noqa: E402

SCHEMA = json.loads((ROOT / "evaluation/aihs/a2-record.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, sort_keys=True).encode())


def make_episode(root: Path, seq: int, *, outcome="success", action="remove_scoped_db_reject", receipt="executed",
                 status="recovered_stable", snapshot=True, late=False, reset=True, executed=True, scenario="DB-01"):
    episode_id = f"ep-{seq:012x}"
    directory = root / "camp" / "episodes" / f"{seq:05d}-{episode_id}"
    base = 1000 + seq * 100
    stamp = lambda s: f"2026-11-15T12:{(base + s) // 60 % 60:02d}:{(base + s) % 60:02d}.000Z"  # noqa: E731
    features = {"moodle_node_web_running__a": 1.0, "synthetic_success": 0.0, "moodle_node_rds_tcp_success__b": None}
    if snapshot:
        parts = {}
        for name, content in (("metrics.json", b'{"samples":{}}'), ("alerts.json", b'{"alerts":[]}'), ("logs/web_a.log", b"line\n")):
            write(directory / "snapshot" / name, content)
            parts[name] = sha(content)
        write(directory / "snapshot/features.json", features)
        feature_sha = sha((directory / "snapshot/features.json").read_bytes())
        cutoff = stamp(10 if not late else 99)
        index = {"schema": "aihs-lab-snapshot-v1", "snapshot_id": "snapshot", "detected_at": stamp(5), "evidence_cutoff_at": cutoff,
                 "capture_started_at": stamp(8), "capture_finished_at": cutoff, "collector_code_sha256": "c" * 64, "parts": parts,
                 "evidence_bundle_sha256": sha(json.dumps(parts, sort_keys=True).encode()), "features_sha256": feature_sha}
        write(directory / "snapshot/snapshot.json", index)
    timeline = {"injected_at": [stamp(0)], "detected_at": stamp(5), "predicted_at": stamp(20), "executed_at": stamp(21),
                "verified_at": stamp(60)}
    if snapshot:
        write(directory / "proposal.json", {"action_id": action, "policy_version": "lab-behavior-policy-v1", "predicted_at": stamp(20)})
    if executed:
        write(directory / "execution.json", {"http_status": 200, "receipt": {"status": receipt, "mutated": True}})
    write(directory / "probes.jsonl", b'{"ok": true}\n')
    episode = {"schema": "aihs-lab-episode-v1", "episode_id": episode_id, "seq": seq, "lab_code_sha256": "d" * 64,
               "plan": {"kind": "single", "faults": [scenario], "background_load": {"name": "none"}, "transient": False, "exploration": False},
               "timeline": timeline, "outcome": outcome, "status": status, "reset_verified": reset,
               "verification": {"stability_window_s": 25.0}}
    write(directory / "episode.json", episode)
    files = {p.relative_to(directory).as_posix(): {"sha256": sha(p.read_bytes()), "bytes": p.stat().st_size}
             for p in sorted(directory.rglob("*")) if p.is_file()}
    write(directory / "manifest.json", {"schema": "aihs-lab-episode-manifest-v1", "files": files,
                                        "files_sha256": sha(json.dumps(files, sort_keys=True).encode())})
    return directory


def export(tmp_path):
    return lab.export_lab(tmp_path / "lab", ["camp"], tmp_path / "out", tmp_path)


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path / "out/audit-records.jsonl").read_text().splitlines()]


def test_lab_records_validate_against_schema_and_are_training_eligible(tmp_path):
    make_episode(tmp_path / "lab", 1)
    make_episode(tmp_path / "lab", 2, outcome="failure", status="probes_not_confirmed")
    manifest = export(tmp_path)
    rows = records(tmp_path)
    assert manifest["candidate_records"] == 2 and manifest["training_eligible"] == 2
    for row in rows:
        VALIDATOR.validate(row)
        assert row["provenance"]["evidence_mode"] == "lab" and row["provenance"]["environment"] == "lab"
        pre = row["pre_decision"]
        assert pre["feature_status"] == "verified_predecision" and pre["snapshot_sha256"] and pre["features_sha256"]
        assert pre["evidence_cutoff_at"] <= row["timeline"]["decision_at"]
        assert all(not FORBIDDEN_FEATURE.search(k) for k in pre["features"])
        assert row["proposal"]["source"] == "other"            # never claims to be an AIHS proposal
        assert row["post_decision"]["gate_decision"] is None   # the lab ran no Safety Gate
    assert manifest["audit_records_sha256"] == sha((tmp_path / "out/audit-records.jsonl").read_bytes())
    assert all(s["sha256"] and s["path"].endswith("manifest.json") for s in manifest["source_files"])


def test_non_repair_outcomes_are_distinct_and_never_training_labels(tmp_path):
    make_episode(tmp_path / "lab", 1, outcome="denied", status="executor_denied", receipt="denied", executed=True)
    make_episode(tmp_path / "lab", 2, outcome="not_executed", status="human_handoff", action="call_human", executed=False)
    make_episode(tmp_path / "lab", 3, outcome="timeout", status="detection_timeout", snapshot=False, executed=False)
    make_episode(tmp_path / "lab", 4, outcome="cancelled", status="cancelled", snapshot=False, executed=False)
    export(tmp_path)
    rows = records(tmp_path)
    assert [r["outcome"] for r in rows] == ["denied", "not_executed", "timeout", "cancelled"]
    for row in rows:
        VALIDATOR.validate(row)
        assert not row["training_eligible"] and row["post_decision"]["executed_action_id"] is None
        assert any(reason.startswith("outcome_") for reason in row["exclusion_reasons"])


def test_late_snapshot_and_unverified_reset_are_excluded(tmp_path):
    make_episode(tmp_path / "lab", 1, late=True)
    make_episode(tmp_path / "lab", 2, reset=False)
    export(tmp_path)
    first, second = records(tmp_path)
    assert first["pre_decision"]["feature_status"] == "invalid_or_late_snapshot" and not first["training_eligible"]
    assert "lab_reset_not_verified" in second["exclusion_reasons"] and not second["training_eligible"]


def test_tampered_episode_is_skipped_not_exported(tmp_path):
    good = make_episode(tmp_path / "lab", 1)
    bad = make_episode(tmp_path / "lab", 2)
    (bad / "probes.jsonl").write_bytes(b'{"ok": false}\n')
    manifest = export(tmp_path)
    assert manifest["candidate_records"] == 1 and manifest["inventory"]["skipped"] == {"unverifiable_episode": 1}
    assert good.exists()


def test_export_has_no_raw_ids_scenario_text_or_sensitive_strings(tmp_path):
    make_episode(tmp_path / "lab", 1)
    export(tmp_path)
    text = (tmp_path / "out/audit-records.jsonl").read_text()
    assert "ep-" not in text and "idempot" not in text.lower() and "signature" not in text.lower()
    row = json.loads(text)
    assert lab.SENSITIVE.search(json.dumps({k: v for k, v in row.items() if k != "scenario_code"})) is None


def test_redact_strips_tokens_signatures_ids_and_pii():
    sample = "token=abc123 signature: SGVsbG8= x@y.example 10.2.3.4 " + "f" * 40
    out = lab.redact(sample)
    for secret in ("abc123", "SGVsbG8=", "x@y.example", "10.2.3.4", "f" * 40):
        assert secret not in out


def test_a_feature_with_an_answer_bearing_name_aborts_the_export(tmp_path):
    directory = make_episode(tmp_path / "lab", 1)
    features = {"scenario_hint": 1.0}
    write(directory / "snapshot/features.json", features)
    index = json.loads((directory / "snapshot/snapshot.json").read_text())
    index["features_sha256"] = sha((directory / "snapshot/features.json").read_bytes())
    write(directory / "snapshot/snapshot.json", index)
    files = {p.relative_to(directory).as_posix(): {"sha256": sha(p.read_bytes()), "bytes": 1}
             for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "manifest.json"}
    write(directory / "manifest.json", {"files": files, "files_sha256": sha(json.dumps(files, sort_keys=True).encode())})
    export(tmp_path)
    row = records(tmp_path)[0]
    assert row["pre_decision"]["feature_status"] == "invalid_or_late_snapshot" and row["pre_decision"]["features"] == {}


def test_staging_export_path_still_works_on_an_empty_tree(tmp_path):
    (tmp_path / "gt").mkdir()
    manifest = a2_export.export(tmp_path / "artifacts", tmp_path / "gt", tmp_path / "out")
    assert manifest["candidate_records"] == 0 and manifest["leakage_scan"].startswith("passed")


def test_label_form_is_empty_in_the_b2_review_shape_and_copies_no_outcome(tmp_path):
    make_episode(tmp_path / "lab", 1)
    make_episode(tmp_path / "lab", 2, outcome="failure", status="probes_not_confirmed")
    export(tmp_path)
    result = lab_label_form.generate(tmp_path / "out/audit-records.jsonl", tmp_path / "out/manifest.json",
                                     tmp_path / "lab", tmp_path, tmp_path / "out")
    assert result["rows"] == 2
    template = [json.loads(line) for line in (tmp_path / "out/independent-label-review.template.jsonl").read_text().splitlines()]
    expected_keys = {"record_id", "outcome", "executed_action_id", "reviewer_id", "evidence_sha256", "reviewed_at"}
    for row in template:
        assert set(row) == expected_keys                          # exactly what b2_freeze_dataset.review_index reads
        assert row["outcome"] is None and row["executed_action_id"] is None
        assert row["reviewer_id"] is None and row["reviewed_at"] is None
        assert len(row["evidence_sha256"]) == 64
    form = [json.loads(line) for line in (tmp_path / "out/independent-label-form.jsonl").read_text().splitlines()]
    blob = json.dumps(form)
    assert "episode.json" not in blob and '"success"' not in blob and '"failure"' not in blob
    for row in form:
        assert all(v is None for v in row["proposal_label"].values()) and all(v is None for v in row["recovery_label"].values())
        assert row["reviewer_id"] is None and "outcome" not in row


def test_unfilled_template_is_rejected_by_freeze_review_loader(tmp_path):
    """A blank template must not be usable as labels: the freeze tool refuses it until a reviewer fills it."""
    import b2_freeze_dataset as b2
    make_episode(tmp_path / "lab", 1)
    export(tmp_path)
    lab_label_form.generate(tmp_path / "out/audit-records.jsonl", tmp_path / "out/manifest.json", tmp_path / "lab", tmp_path, tmp_path / "out")
    rows = [json.loads(line) for line in (tmp_path / "out/audit-records.jsonl").read_text().splitlines()]
    with pytest.raises(ValueError):
        b2.review_index(rows, tmp_path / "out/independent-label-review.template.jsonl")
