"""Train / lock / propose / reproducibility check. Test labels are never read here (sealed until after the lock).

Nothing in this module executes an action: a proposal is an untrusted suggestion for the later Safety Gate stage.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import AS_IS_BASELINE_ID, PAPER_BASELINE_ID, PIPELINE_VERSION
from .data import (SPLITS, Splits, SplitError, read_records, record_hash, sha256_file, sha256_text)
from .features import FEATURE_SPEC_VERSION, feature_spec, feature_spec_sha256
from .mapper import DOC_TOPIC_PRIOR, MAX_ITER, TOPIC_WORD_PRIOR
from .metrics import calibration_note, evaluate
from .model import MIN_CONTROLLER_PER_CLASS, MoodleAIHSModel, lgbm_params

K_GRID = (5, 10, 20)
AS_IS_K = 10
DEFAULT_SEED = 20261115
REPO = Path(__file__).resolve().parents[3]
CODE_FILES = ("evaluation/aihs/action_ladder.py", "evaluation/aihs/a2_split.py")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------------ splits
def splits_from_manifest(records: list[dict], manifest: dict) -> Splits:
    """Rebuild the split from a split manifest, verifying every record hash (detects edited/missing records)."""
    by_id = {r["record_id"]: r for r in records}
    parts = {}
    for name in SPLITS:
        info = manifest["splits"][name]
        recs = []
        for rid in info["record_ids"]:
            if rid not in by_id or record_hash(by_id[rid]) != info["record_hashes"][rid]:
                raise SplitError(f"record {rid} is missing or differs from the split manifest")
            recs.append(by_id[rid])
        parts[name] = recs
    return Splits({"mode": manifest["mode"], "records": parts, "groups": {n: manifest["splits"][n]["groups"] for n in SPLITS}})


def load_splits(records_path: Path, manifest_path: Path) -> tuple[Splits, dict]:
    manifest = json.loads(Path(manifest_path).read_text())
    if sha256_file(records_path) != manifest["records_sha256"]:
        raise SplitError("records file differs from the split manifest")
    return splits_from_manifest(read_records(records_path), manifest), manifest


# ------------------------------------------------------------------ train
def fit_with_k_selection(splits: Splits, baseline_id: str, seed: int, k_grid=K_GRID) -> tuple[MoodleAIHSModel, list[dict]]:
    """K is chosen on VALIDATION only (primary: OverallRate; tie-break: lower held-out perplexity, then smaller K).
    The Mapper (vocabulary, LDA) is fitted on TRAIN only for every K."""
    train, valid = splits.rows("train"), splits.rows("validation")
    grid = (AS_IS_K,) if baseline_id == AS_IS_BASELINE_ID else tuple(k_grid)
    table, best, best_key = [], None, None
    for k in grid:
        model = MoodleAIHSModel(baseline_id, k, seed).fit(train)
        val = evaluate(model, splits, "validation")
        perplexity = model.mapper.perplexity([r["features"] for r in valid])
        overall = val["OverallRate"]
        table.append({"K": k, "validation_OverallRate": overall, "validation_HitRate": val["HitRate"],
                      "validation_ReverseRate": val["ReverseRate"], "validation_perplexity": perplexity})
        key = (-1.0 if overall is None else overall, -perplexity, -k)
        if best_key is None or key > best_key:
            best, best_key = model, key
    for entry in table:
        entry["selected"] = entry["K"] == best.n_topics
    return best, table


def save_model(model: MoodleAIHSModel, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "model.pkl"
    # sklearn's CountVectorizer caches id(stop_words) (a memory address) in its state; zero it so the pickle bytes
    # are identical across processes (it is only a cache key and is recomputed on the next transform).
    model.mapper.vectorizer._stop_words_id = 0
    path.write_bytes(pickle.dumps({"pipeline": PIPELINE_VERSION, "model": model}, protocol=4))
    return path


def load_model(directory: Path, lock: dict | None = None) -> MoodleAIHSModel:
    """Unpickles only after the file hash matches the lock; never unpickle an unverified file."""
    path = directory / "model.pkl"
    if lock is None:
        lock = json.loads((directory / "model-lock.json").read_text())
    if sha256_file(path) != lock["model_file_sha256"]:
        raise ValueError("model.pkl does not match model-lock.json; refusing to unpickle")
    model = pickle.loads(path.read_bytes())["model"]
    if model.content_digest() != lock["model_content_sha256"]:
        raise ValueError("model content digest differs from the lock")
    return model


def train(records_path: Path, manifest_path: Path, baseline_id: str, out_dir: Path, seed: int = DEFAULT_SEED,
          k_grid=K_GRID) -> dict:
    splits, manifest = load_splits(records_path, manifest_path)
    model, table = fit_with_k_selection(splits, baseline_id, seed, k_grid)
    save_model(model, out_dir)
    report = {
        "stage": "phase2_train", "baseline_id": baseline_id, "mode": splits.mode, "seed": seed,
        "notice": ("DEV / not frozen / runner-outcome labels (no independent labels). Train/validation only; "
                   "the test split was never scored." if splits.mode == "DEV" else
                   "Train/validation only; the test split was never scored."),
        "split": {n: manifest["splits"][n]["records"] for n in SPLITS} | {"groups": {n: manifest["splits"][n]["groups"] for n in SPLITS}},
        "selected_K": model.n_topics, "k_selection_on_validation": table,
        "train_in_sample": evaluate(model, splits, "train"),
        "validation": evaluate(model, splits, "validation"),
        "training_counts": model.counts, "controller_coverage": model.coverage,
        "predictor_classes_observed": model.predictor_classes,
        "predictor_classes_absent": sorted(set(_ladder_classes()) - set(model.predictor_classes)),
        "limitations": model.limitations,
        "calibration_validation": calibration_note(model, splits, "validation"),
        "model_content_sha256": model.content_digest(),
        "model_file_sha256": sha256_file(out_dir / "model.pkl"),
    }
    (out_dir / "train-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def _ladder_classes():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from action_ladder import PREDICTOR_CLASSES
    return PREDICTOR_CLASSES


# ------------------------------------------------------------------ lock
def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False).stdout


def code_identity() -> dict:
    sha = _git("rev-parse", "HEAD").strip()
    porcelain = _git("status", "--porcelain", "--", "evaluation/aihs", "agent_src").strip()
    diff = _git("diff", "HEAD", "--", "evaluation/aihs", "agent_src")
    untracked = sorted(p for p in _git("ls-files", "-o", "--exclude-standard", "--", "evaluation/aihs", "agent_src").splitlines() if p)
    h = hashlib.sha256(diff.encode())
    for p in untracked:
        h.update(p.encode())
        h.update((REPO / p).read_bytes())
    files = {p: sha256_file(REPO / p) for p in CODE_FILES}
    package = REPO / "evaluation/aihs/moodle_aihs"
    files.update({f"evaluation/aihs/moodle_aihs/{p.name}": sha256_file(p) for p in sorted(package.glob("*.py"))})
    files["evaluation/aihs/train_moodle_aihs.py"] = sha256_file(REPO / "evaluation/aihs/train_moodle_aihs.py")
    return {"git_sha": sha, "dirty": bool(porcelain), "dirty_diff_sha256": h.hexdigest(),
            "dirty_paths": [line[3:] for line in porcelain.splitlines()], "code_files_sha256": files,
            "code_files_digest": sha256_text(json.dumps(files, sort_keys=True))}


def dependency_versions() -> dict:
    import joblib, lightgbm, numpy, scipy, sklearn
    return {"python": platform.python_version(), "scikit-learn": sklearn.__version__, "lightgbm": lightgbm.__version__,
            "numpy": numpy.__version__, "scipy": scipy.__version__, "joblib": joblib.__version__}


def lock(model_dir: Path, records_path: Path, manifest_path: Path, report: dict | None = None) -> dict:
    report = report or json.loads((model_dir / "train-report.json").read_text())
    splits, manifest = load_splits(records_path, manifest_path)
    model = pickle.loads((model_dir / "model.pkl").read_bytes())["model"]   # we just wrote it; hash recorded below
    dev = splits.mode == "DEV"
    names = sorted(model.mapper.discretizer.edges)
    result = {
        "schema_version": 1, "stage": "phase2_lock",
        "status": ("DEV_MODEL_NOT_FROZEN_C0" if dev else
                   "LOCKED_REFERENCE_NOT_C0" if model.baseline_id == AS_IS_BASELINE_ID else "LOCKED_C0"),
        "dev_notice": ("DEV model trained without independent labels on runner outcomes. NEVER present it as the "
                       "frozen C0." if dev else None),
        "baseline_id": model.baseline_id, "pipeline_version": PIPELINE_VERSION, "locked_at": now_utc(),
        "code": code_identity(), "dependencies": dependency_versions(),
        "seeds": {"global": model.seed, "lda_random_state": model.seed, "lightgbm_random_state": model.seed},
        "K": model.n_topics, "k_grid": [e["K"] for e in report["k_selection_on_validation"]],
        "k_selection_on_validation": report["k_selection_on_validation"],
        "lda": {"implementation": "sklearn.decomposition.LatentDirichletAllocation", "learning_method": "batch",
                "doc_topic_prior": DOC_TOPIC_PRIOR, "topic_word_prior": TOPIC_WORD_PRIOR, "max_iter": MAX_ITER,
                "deviation_from_paper": "variational Bayes (sklearn) instead of collapsed Gibbs sampling (1024 sweeps)"},
        "lightgbm_params": lgbm_params(model.seed), "min_controller_rows_per_class": MIN_CONTROLLER_PER_CLASS,
        "feature_spec_version": FEATURE_SPEC_VERSION, "feature_spec": feature_spec(),
        "feature_spec_sha256": feature_spec_sha256(),
        "feature_names_sha256": sha256_text(json.dumps(names)), "feature_name_count": len(names),
        "vocabulary_sha256": model.mapper.vocabulary_sha256(), "vocabulary_size": len(model.mapper.vocabulary),
        "model_file": "model.pkl", "model_file_sha256": sha256_file(model_dir / "model.pkl"),
        "model_content_sha256": model.content_digest(),
        "split_manifest_sha256": sha256_file(manifest_path), "split_ids_sha256": manifest["split_ids_sha256"],
        "records_sha256": manifest["records_sha256"], "split_mode": splits.mode,
        "freeze_id": manifest.get("freeze_id"),
        "training_counts": model.counts, "controller_coverage": model.coverage, "limitations": model.limitations,
        "fit_policy": "vocabulary, discretizer edges and LDA fitted on TRAIN only; K chosen on VALIDATION only; test never read",
        "model_output_policy": "untrusted proposal; no execution; passes the Safety Gate in a later stage",
    }
    (model_dir / "model-lock.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


# ------------------------------------------------------------------ propose
def proposals_digest(rows: list[dict]) -> str:
    stable = [{k: v for k, v in r.items() if k != "predicted_at"} for r in rows]
    return sha256_text(json.dumps(stable, sort_keys=True))


def propose(model_dir: Path, records_path: Path, manifest_path: Path, out_path: Path | None = None) -> dict:
    """Run the LOCKED model on every test snapshot. Writes proposals only: no labels opened, nothing scored."""
    lock_data = json.loads((model_dir / "model-lock.json").read_text())
    if sha256_file(manifest_path) != lock_data["split_manifest_sha256"]:
        raise ValueError("split manifest differs from the one in the lock")
    model = load_model(model_dir, lock_data)
    splits, manifest = load_splits(records_path, manifest_path)
    version = f"{model.baseline_id}+{lock_data['model_content_sha256'][:16]}"
    rows = []
    for snap in splits.test_snapshots():                    # features and snapshot metadata only
        result = model.propose(snap["features"])
        predicted_at = now_utc()
        if snap["evidence_cutoff_at"] and predicted_at < snap["evidence_cutoff_at"]:
            raise ValueError("prediction time precedes the evidence cutoff")
        rows.append({"record_id": snap["record_id"], "split": "test", "model_version": version,
                     "baseline_id": model.baseline_id, "status": lock_data["status"],
                     "snapshot_sha256": snap["snapshot_sha256"], "evidence_cutoff_at": snap["evidence_cutoff_at"],
                     "predicted_at": predicted_at, "proposed_action": result["proposed_action"],
                     "predictor_action": result["predictor_action"], "path": result["path"],
                     "controller_trace": result["controller_trace"], "record_hash": snap["record_hash"]})
    out_path = out_path or (model_dir / "proposals-test.jsonl")
    out_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    summary = {"stage": "phase2_propose", "status": lock_data["status"], "proposals": len(rows),
               "proposals_file": str(out_path).replace("\\", "/"), "proposals_file_sha256": sha256_file(out_path),
               "proposals_content_sha256": proposals_digest(rows), "model_content_sha256": lock_data["model_content_sha256"],
               "scored": False, "labels_opened": False}
    (out_path.parent / "proposals-test-manifest.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


# ------------------------------------------------------------------ reproducibility
def reproduce(model_dir: Path, records_path: Path, manifest_path: Path) -> dict:
    """Retrain from scratch with the locked inputs and compare model hashes and proposals."""
    lock_data = json.loads((model_dir / "model-lock.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        report = train(records_path, manifest_path, lock_data["baseline_id"], tmp_dir, lock_data["seeds"]["global"],
                       tuple(lock_data["k_grid"]))
        copy_lock = dict(lock_data, model_file_sha256=report["model_file_sha256"],
                         model_content_sha256=report["model_content_sha256"])
        (tmp_dir / "model-lock.json").write_text(json.dumps(copy_lock))
        again = propose(tmp_dir, records_path, manifest_path) if sha256_file(manifest_path) == lock_data["split_manifest_sha256"] else None
    original = [json.loads(line) for line in (model_dir / "proposals-test.jsonl").read_text().splitlines()] \
        if (model_dir / "proposals-test.jsonl").is_file() else None
    result = {
        "model_content_sha256_same": report["model_content_sha256"] == lock_data["model_content_sha256"],
        "model_file_sha256_same": report["model_file_sha256"] == lock_data["model_file_sha256"],
        "retrained_model_content_sha256": report["model_content_sha256"],
        "locked_model_content_sha256": lock_data["model_content_sha256"],
        "proposals_same": (again is not None and original is not None
                           and again["proposals_content_sha256"] == proposals_digest(original)),
    }
    result["reproducible"] = all((result["model_content_sha256_same"], result["model_file_sha256_same"], result["proposals_same"]))
    return result
