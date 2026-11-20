"""Records -> model rows, time-ordered grouped split, split manifest and the sealed test split.

Reuses ``a2_split.eligible`` / ``grouped_chronological_split`` (connected incident OR session groups, ordered by the
earliest decision time) and the 70/15/15 group cut of ``b2_freeze_dataset.py``. Because boundaries are global time
cuts between whole groups, every lab worker's own episode order is preserved and no session crosses a boundary;
an ambiguous-pair episode is one record in one group. The audit-only ``episode`` block is read only to flag
transient/exploration episodes; it never becomes a feature.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from a2_split import eligible, grouped_chronological_split, parse_time  # noqa: E402

from .features import check_feature_names  # noqa: E402
from .purge import purge_temporal_overlap  # noqa: E402

SPLITS = ("train", "validation", "test")
EVAL = Path(__file__).resolve().parents[1]


class SealedTestError(RuntimeError):
    """Raised when something tries to read test labels / score the test split before a model is locked."""


class SplitError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def record_hash(record: dict) -> str:
    return sha256_text(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


def to_row(record: dict) -> dict:
    """Model row. ``features``/``snapshot`` are the only decision inputs; the rest is label/audit data."""
    pre, episode = record["pre_decision"], record.get("episode") or {}
    check_feature_names(pre["features"])
    return {
        "record_id": record["record_id"],
        "features": dict(pre["features"]),
        "snapshot_sha256": pre.get("snapshot_sha256"),
        "evidence_cutoff_at": pre.get("evidence_cutoff_at") or pre.get("snapshot_at"),
        "captured_at": pre.get("captured_at"),
        "decision_at": record["timeline"]["decision_at"],
        # label side (never an input of the Mapper/Predictor at inference time)
        "action": record["post_decision"]["executed_action_id"],
        "outcome": record["outcome"],
        "transient": bool(episode.get("transient", False)),
        "exploration": bool(episode.get("exploration", False)),
        "record_hash": record_hash(record),
    }


def _group_split(selected: list[dict], mode_name: str) -> dict[str, list[dict]]:
    groups = grouped_chronological_split(selected)
    n = len(groups)
    if n < 3:
        raise SplitError(f"{mode_name}: need at least 3 incident/session groups for train/validation/test, have {n}")
    train_end, valid_end = int(n * 0.70), int(n * 0.85)
    chunks = {"train": groups[:train_end], "validation": groups[train_end:valid_end], "test": groups[valid_end:]}
    if any(not v for v in chunks.values()):
        raise SplitError(f"{mode_name}: empty split with {n} groups")
    chunks, purged = purge_temporal_overlap(chunks)
    if any(not v for v in chunks.values()):
        raise SplitError(f"{mode_name}: a split is empty after purging time-overlapping groups")
    out = {k: [r for g in v for r in g] for k, v in chunks.items()}
    bounds = {k: (min(parse_time(r["timeline"]["decision_at"]) for r in v),
                  max(parse_time(r["timeline"]["decision_at"]) for r in v)) for k, v in out.items()}
    if not (bounds["train"][1] < bounds["validation"][0] and bounds["validation"][1] < bounds["test"][0]):
        raise SplitError("incident/session groups overlap in time across a split boundary")
    out["_groups"] = {k: len(v) for k, v in chunks.items()}
    out["_purged"] = [r["record_id"] for g in purged for r in g]
    return out


def dev_split(records: list[dict]) -> dict:
    """DEV split computed locally from eligible (executed success/failure) rows. No independent labels needed."""
    selected = [r for r in records if eligible(r)]
    parts = _group_split(selected, "dev")
    return {"mode": "DEV", "groups": parts.pop("_groups"), "purged_record_ids": parts.pop("_purged"),
            "records": parts, "eligible": len(selected)}


def frozen_split(records: list[dict], freeze_manifest: Path) -> dict:
    """Official split: membership comes from the READY b2_freeze_dataset.py output; hashes are re-verified."""
    manifest = json.loads(Path(freeze_manifest).read_text())
    if manifest.get("status") != "READY" or not manifest.get("splits"):
        raise SplitError(f"freeze manifest is {manifest.get('status')!r}: {manifest.get('reasons')}")
    frozen_dir = Path(manifest["frozen_data_local"])
    if not frozen_dir.is_absolute():
        frozen_dir = EVAL.parents[1] / frozen_dir
    by_id = {r["record_id"]: r for r in records}
    parts = {}
    for name in SPLITS:
        info = manifest["splits"][name]
        feature_file = frozen_dir / f"{name}-features.jsonl"
        if sha256_file(feature_file) != info["features_sha256"]:
            raise SplitError(f"{name}-features.jsonl hash differs from the freeze manifest")
        ids = [json.loads(line)["record_id"] for line in feature_file.read_text().splitlines() if line]
        parts[name] = [by_id[i] for i in ids]
        if name != "test" and sha256_file(frozen_dir / f"{name}-labels.jsonl") != info["labels_sha256"]:
            raise SplitError(f"{name}-labels.jsonl hash differs from the freeze manifest")
    return {"mode": "FROZEN", "freeze_id": manifest["freeze_id"], "freeze_manifest_sha256": sha256_file(freeze_manifest),
            "groups": {k: manifest["splits"][k]["groups"] for k in SPLITS},
            "purged_record_ids": manifest.get("purged_records", []), "records": parts,
            "eligible": manifest["selected_records"]}


def split_manifest(split: dict, records_path: Path, records: list[dict]) -> dict:
    rows = {name: [to_row(r) for r in recs] for name, recs in split["records"].items()}
    ids = [r["record_id"] for v in rows.values() for r in v]
    if len(ids) != len(set(ids)):
        raise SplitError("a record appears in more than one split")
    sessions = {name: {r["session_group_id"] for r in recs} for name, recs in split["records"].items()}
    incidents = {name: {r["incident_group_id"] for r in recs} for name, recs in split["records"].items()}
    for a in SPLITS:
        for b in SPLITS:
            if a < b and (sessions[a] & sessions[b] or incidents[a] & incidents[b]):
                raise SplitError("an incident or session group crosses a split boundary")
    out = {
        "schema_version": 1, "stage": "phase2_split", "mode": split["mode"],
        "policy": "chronological_70_15_15_by_connected_incident_session_groups (a2_split.grouped_chronological_split)",
        "dev_notice": ("DEV split: computed locally without independent labels; outcomes are runner outcomes. "
                       "Not the frozen evaluation set." if split["mode"] == "DEV" else None),
        "freeze_id": split.get("freeze_id"), "freeze_manifest_sha256": split.get("freeze_manifest_sha256"),
        "records_file": str(records_path).replace("\\", "/"), "records_sha256": sha256_file(records_path),
        "total_audit_records": len(records), "eligible_records": split["eligible"],
        "per_worker_order_preserved": "yes_by_construction: global time cuts between whole groups",
        "ambiguous_pair_and_session_groups_never_cross": True,
        "purged_overlapping_records": split.get("purged_record_ids", []),
        "purge_policy": "groups of the earlier split that overlap in decision time with the next split are removed (embargo)",
        "test_sealed": True,
        "splits": {},
    }
    for name in SPLITS:
        recs = split["records"][name]
        times = sorted(parse_time(r["timeline"]["decision_at"]) for r in recs)
        out["splits"][name] = {
            "groups": split["groups"][name], "records": len(recs),
            "decision_at_range": [times[0].isoformat(), times[-1].isoformat()],
            "record_ids": [r["record_id"] for r in recs],
            "record_hashes": {r["record_id"]: record_hash(r) for r in recs},
        }
    out["split_ids_sha256"] = sha256_text(json.dumps({n: out["splits"][n]["record_hashes"] for n in SPLITS}, sort_keys=True))
    return out


class Splits:
    """Holds the three splits. Test labels are sealed: only ``test_snapshots()`` (features + hashes) is readable."""

    def __init__(self, split: dict):
        self.mode = split["mode"]
        self.meta = split
        self._rows = {n: [to_row(r) for r in recs] for n, recs in split["records"].items()}

    def rows(self, name: str, lock: dict | None = None) -> list[dict]:
        if name == "test" and not lock:
            raise SealedTestError("test split is sealed: labels/metrics are only available after the model is locked")
        return self._rows[name]

    def test_snapshots(self) -> list[dict]:
        """Features + snapshot hash + cutoff only. No outcome, action, transient or scenario field."""
        keys = ("record_id", "features", "snapshot_sha256", "evidence_cutoff_at", "captured_at", "record_hash")
        return [{k: r[k] for k in keys} for r in self._rows["test"]]
