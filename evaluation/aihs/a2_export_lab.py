"""Lab-campaign reader for the A2 exporter (used by a2_export.py --source lab).

Reads Git-ignored lab artifacts (evaluation/aihs/.artifacts/lab-campaign/<campaign>/episodes/*), verifies every episode
checksum, and emits records valid against a2-record.schema.json. Lab data is exported on its own and is never merged
with staging records. Raw evidence is read only to rebuild the pre-decision feature row; scenario markers, ground truth
(`plan`), run ids and signature/approval material stay in the artifacts as audit data and never enter a feature or a
record field other than the audit-only `scenario_code` / `episode` blocks.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ACTION = re.compile(r"^[A-Za-z_]{1,64}$")
SENSITIVE = re.compile(
    r"-----BEGIN|arn:aws:|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:AKIA|ASIA)[A-Z0-9]{16}"
)
# Same forbidden-name rule as b2_freeze_dataset.py plus lab-specific answer-bearing words.
FORBIDDEN_FEATURE = re.compile(
    r"scenario|ground.?truth|outcome|verifier|gate|approval|receipt|action|incident|run.?id|fault|answer|label|post.?decision"
    r"|signature|idempot|episode|marker|injected|reset|baseline|variant|transient|exploration|behavior|policy",
    re.IGNORECASE)
FORBIDDEN_TEXT = re.compile(
    r"\b(?:DB|NET|RES|CON|SEC|BIZ)-0\d\b|moodle[-_]fault|ground[_ -]?truth|idempotency|action_sha256|approval"
    r"|\bep-[0-9a-f]{8,}\b|run[_-]?id", re.IGNORECASE)
REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "<redacted:key>"),
    (re.compile(r"(?i)\b(bearer|token|sesskey|password|passwd|secret|authorization|signature)\b(\s*[=:]\s*)(?:(?:bearer|basic)\s+)?\S+"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<redacted:email>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<redacted:hex>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
]
KNOWN_STATUS = {
    "recovered_stable", "probes_not_confirmed", "human_handoff", "detection_timeout", "cancelled", "executor_error",
    "executor_timeout", "executor_denied", "executor_awaiting_approval", "executor_disabled", "baseline_not_clean",
    "leakage_blocked", "runner_exception",
}
RECEIPT_STATUS = {"executed", "partial_failure", "failed", "denied", "awaiting_approval", "disabled"}
WAIT_ACTION = "wait_and_recheck"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redact(text: str) -> str:
    """Redact tokens, signatures, identifiers and PII-like strings from free text before it can be exported."""
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _action(value):
    return value if isinstance(value, str) and ACTION.fullmatch(value) else None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def verify_episode_manifest(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, meta in manifest["files"].items():
        target = directory / name
        if not target.is_file() or sha256(target) != meta["sha256"]:
            raise ValueError(f"lab episode checksum mismatch: {directory.name}/{name}")
    return manifest


def _load(path: Path):
    return json.loads(path.read_text()) if path.is_file() else None


def lab_record(number: int, group_id: str, session_id: str, directory: Path, campaign: str, repo_root: Path):
    """Return (record, source entry, raw episode id) for one verified episode directory."""
    episode = json.loads((directory / "episode.json").read_text())
    manifest = verify_episode_manifest(directory)
    plan = episode["plan"]
    timeline = episode.get("timeline") or {}
    outcome = episode["outcome"]
    proposal = _load(directory / "proposal.json")
    execution = _load(directory / "execution.json")
    snapshot_file = directory / "snapshot" / "snapshot.json"
    reasons: list[str] = []
    pre = {"snapshot_at": None, "feature_status": "missing_predecision_snapshot", "evidence_ref_hashes": [], "features": {}}
    if snapshot_file.is_file():
        index = json.loads(snapshot_file.read_text())
        features = json.loads((directory / "snapshot" / "features.json").read_text())
        parts_ok = all(sha256(directory / "snapshot" / name) == digest for name, digest in index["parts"].items())
        names_ok = not any(FORBIDDEN_FEATURE.search(k) or FORBIDDEN_TEXT.search(k) for k in features)
        decision_at = timeline.get("predicted_at")
        cutoff_ok = bool(decision_at) and index["detected_at"] <= index["evidence_cutoff_at"] <= decision_at
        features_ok = sha256(directory / "snapshot" / "features.json") == index["features_sha256"]
        if parts_ok and names_ok and cutoff_ok and features_ok:
            pre = {"snapshot_at": index["evidence_cutoff_at"], "evidence_cutoff_at": index["evidence_cutoff_at"],
                   "captured_at": index["capture_finished_at"], "snapshot_sha256": index["evidence_bundle_sha256"],
                   "features_sha256": index["features_sha256"], "feature_status": "verified_predecision",
                   "evidence_ref_hashes": sorted(set(index["parts"].values())),
                   "features": {k: (None if v is None else float(v)) for k, v in features.items()}}
        else:
            pre["feature_status"] = "invalid_or_late_snapshot"
            reasons.append("invalid_or_late_snapshot")
    else:
        reasons.append("missing_predecision_snapshot")

    receipt = (execution or {}).get("receipt") or {}
    action_id = _action(proposal.get("action_id")) if proposal else None
    if action_id == WAIT_ACTION and outcome in ("success", "failure"):
        receipt_status, executed_action = "executed", action_id       # observe-only: mutated=false
    elif execution:
        status = receipt.get("status")
        receipt_status = status if status in RECEIPT_STATUS else None
        executed_action = action_id if status == "executed" else None
    else:
        receipt_status, executed_action = None, None
    if outcome in ("success", "failure") and not executed_action:
        outcome = "missing_evidence"
    if outcome not in ("success", "failure"):
        reasons.append(f"outcome_{outcome}_is_not_a_repair_label")
    if not episode.get("reset_verified"):
        reasons.append("lab_reset_not_verified")
    verifier = None
    if executed_action and outcome in ("success", "failure"):
        verifier = "lab_probe_stability_confirmed" if outcome == "success" else "lab_probe_stability_not_confirmed"
    detected = timeline.get("detected_at")
    record = {
        "record_id": f"a2-{number:04d}", "incident_group_id": group_id, "session_group_id": session_id,
        "scenario_code": plan["faults"][0],
        "provenance": {"evidence_mode": "lab", "environment": "lab", "campaign_ref": campaign,
                       "source_sha256": manifest["files_sha256"],
                       "historical_code_version": "lab-" + episode["lab_code_sha256"][:16]},
        "timeline": {"detected_at": detected, "decision_at": timeline.get("predicted_at"),
                     "predicted_at": timeline.get("predicted_at"),
                     "executed_at": timeline.get("executed_at") if executed_action else None,
                     "verified_at": timeline.get("verified_at") if verifier else None},
        "pre_decision": pre,
        "proposal": {"source": "other", "action_id": action_id,
                     "model_version": proposal.get("policy_version") if proposal else None},
        "post_decision": {"gate_decision": None, "operator_approved": False, "executed_action_id": executed_action,
                          "receipt_status": receipt_status, "verifier_verdict": verifier,
                          "runtime_status": episode.get("status") if episode.get("status") in KNOWN_STATUS else None},
        "outcome": outcome,
        "episode": {"kind": plan["kind"], "fault_count": len(plan["faults"]),
                    "background_load": plan["background_load"]["name"], "transient": bool(plan["transient"]),
                    "exploration": bool(plan["exploration"]),
                    "stability_window_seconds": (episode.get("verification") or {}).get("stability_window_s")},
        "training_eligible": (not reasons) and pre["feature_status"] == "verified_predecision",
        "exclusion_reasons": sorted(set(reasons)),
    }
    source = {"path": _rel(directory / "manifest.json", repo_root), "sha256": sha256(directory / "manifest.json"),
              "evidence_class": "lab_episode", "historical_code_version": record["provenance"]["historical_code_version"]}
    return record, source, episode["episode_id"]


def export_lab(lab_root: Path, campaigns: list[str], output_dir: Path, repo_root: Path, session_size: int = 8) -> dict:
    """Write audit-records.jsonl and manifest.json for the lab campaigns; staging inputs are never read here."""
    directories = []
    skipped: Counter = Counter()
    for campaign in campaigns:
        for directory in sorted((lab_root / campaign / "episodes").glob("*")):
            if not (directory / "manifest.json").is_file() or not (directory / "episode.json").is_file():
                skipped["incomplete_episode_directory"] += 1
                continue
            directories.append((campaign, directory))

    def started(item):
        episode = json.loads((item[1] / "episode.json").read_text())
        return (episode["timeline"].get("injected_at") or [""])[0], episode["episode_id"]

    directories.sort(key=started)
    records, sources, raw_identifiers = [], [], set()
    for index, (campaign, directory) in enumerate(directories):
        try:
            record, source, episode_id = lab_record(len(records) + 1, f"g{index + 1:04d}", f"s{index // session_size + 1:04d}",
                                                    directory, campaign, repo_root)
        except (ValueError, KeyError, json.JSONDecodeError):
            skipped["unverifiable_episode"] += 1
            continue
        raw_identifiers.add(episode_id)
        records.append(record)
        sources.append(source)
    for record in records:
        features = record["pre_decision"]["features"]
        if any(FORBIDDEN_FEATURE.search(k) or FORBIDDEN_TEXT.search(k) for k in features):
            raise ValueError("post-decision or answer-bearing feature name")
        if record["training_eligible"] and not features:
            raise ValueError("training row without pre-decision features")
    serialized = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    # scenario_code and episode metadata are audit-only blocks; every other field must be free of leaks.
    scan = "".join(json.dumps({k: v for k, v in r.items() if k not in ("scenario_code", "episode")}, sort_keys=True)
                   for r in records)
    if SENSITIVE.search(scan) or FORBIDDEN_TEXT.search(scan) or any(x in serialized for x in raw_identifiers):
        raise ValueError("sensitive content, scenario marker or raw identifier leaked into export")
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "audit-records.jsonl"
    audit_path.write_text(serialized)
    triples = Counter((r["scenario_code"], r["proposal"]["action_id"] or "unknown", r["outcome"]) for r in records)
    manifest = {
        "schema_version": 1, "evidence_mode": "lab",
        "source_policy": "complete lab episode directories whose manifest checksums all verify; never merged with staging",
        "inventory": {"campaigns": sorted(campaigns), "episode_directories": len(directories),
                      "skipped": dict(sorted(skipped.items()))},
        "classifications": {"lab_episode": len(records)},
        "source_files": sources, "distinct_incidents": len(records), "candidate_records": len(records),
        "by_proposal_source": dict(sorted(Counter(r["proposal"]["source"] for r in records).items())),
        "by_scenario": dict(sorted(Counter(r["scenario_code"] for r in records).items())),
        "by_action": dict(sorted(Counter(r["proposal"]["action_id"] or "unknown" for r in records).items())),
        "by_outcome": dict(sorted(Counter(r["outcome"] for r in records).items())),
        "by_scenario_action_outcome": {f"{a}|{b}|{c}": n for (a, b, c), n in sorted(triples.items())},
        "training_eligible": sum(r["training_eligible"] for r in records),
        "leakage_scan": "passed_no_raw_identifiers_scenario_markers_or_sensitive_patterns",
        "audit_records_sha256": sha256(audit_path),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest
