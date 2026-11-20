"""CLI for the Moodle AIHS baseline (C0): split -> train -> lock -> propose -> repro-check.

Baselines: ``aihs-paper-repro-moodle-v1`` (default, the C0) and ``aihs-public-prototype-as-is-v1`` (reference only).
The test split is sealed: `train` and `evaluate` never score it; `propose` writes proposals only.

  python evaluation/aihs/train_moodle_aihs.py split  --records R --out-dir D (--freeze-manifest F | --dev-without-independent-labels)
  python evaluation/aihs/train_moodle_aihs.py train  --records R --split-manifest D/split-manifest.json --out-dir D/paper
  python evaluation/aihs/train_moodle_aihs.py lock   --records R --split-manifest ... --model-dir D/paper
  python evaluation/aihs/train_moodle_aihs.py propose --records R --split-manifest ... --model-dir D/paper
  python evaluation/aihs/train_moodle_aihs.py repro-check --records R --split-manifest ... --model-dir D/paper
  python evaluation/aihs/train_moodle_aihs.py evaluate --split validation ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from moodle_aihs import AS_IS_BASELINE_ID, PAPER_BASELINE_ID  # noqa: E402
from moodle_aihs import data, pipeline  # noqa: E402
from moodle_aihs.metrics import evaluate  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, model_dir=False):
        p.add_argument("--records", type=Path, required=True)
        if model_dir:
            p.add_argument("--split-manifest", type=Path, required=True)
            p.add_argument("--model-dir", type=Path, required=True)

    p = sub.add_parser("split")
    common(p)
    p.add_argument("--out-dir", type=Path, required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--freeze-manifest", type=Path, help="READY manifest written by b2_freeze_dataset.py (official)")
    group.add_argument("--dev-without-independent-labels", action="store_true", help="DEV split; never the frozen C0")

    p = sub.add_parser("train")
    common(p)
    p.add_argument("--split-manifest", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--baseline", choices=("paper", "as-is"), default="paper")
    p.add_argument("--seed", type=int, default=pipeline.DEFAULT_SEED)
    for name in ("lock", "propose", "repro-check"):
        common(sub.add_parser(name), model_dir=True)
    p = sub.add_parser("evaluate")
    common(p, model_dir=True)
    p.add_argument("--split", choices=("train", "validation", "test"), required=True)
    args = parser.parse_args(argv)

    if args.command == "split":
        records = data.read_records(args.records)
        try:
            split = (data.frozen_split(records, args.freeze_manifest) if args.freeze_manifest
                     else data.dev_split(records))
            manifest = data.split_manifest(split, args.records, records)
        except data.SplitError as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 3
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "split-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(json.dumps({"mode": manifest["mode"], "eligible_records": manifest["eligible_records"],
                          "splits": {n: {k: manifest["splits"][n][k] for k in ("groups", "records")} for n in data.SPLITS},
                          "split_ids_sha256": manifest["split_ids_sha256"]}))
    elif args.command == "train":
        baseline = PAPER_BASELINE_ID if args.baseline == "paper" else AS_IS_BASELINE_ID
        report = pipeline.train(args.records, args.split_manifest, baseline, args.out_dir, args.seed)
        print(json.dumps({k: report[k] for k in ("baseline_id", "mode", "selected_K", "validation", "model_content_sha256")},
                         indent=1, default=str))
    elif args.command == "lock":
        print(json.dumps({k: v for k, v in pipeline.lock(args.model_dir, args.records, args.split_manifest).items()
                          if k in ("status", "baseline_id", "K", "model_file_sha256", "model_content_sha256", "locked_at")}))
    elif args.command == "propose":
        print(json.dumps(pipeline.propose(args.model_dir, args.records, args.split_manifest)))
    elif args.command == "repro-check":
        result = pipeline.reproduce(args.model_dir, args.records, args.split_manifest)
        print(json.dumps(result))
        return 0 if result["reproducible"] else 4
    elif args.command == "evaluate":
        splits, _ = pipeline.load_splits(args.records, args.split_manifest)
        lock_path = args.model_dir / "model-lock.json"
        lock_data = json.loads(lock_path.read_text()) if lock_path.is_file() else None
        model = pipeline.load_model(args.model_dir, lock_data) if lock_data else None
        if model is None:
            print("evaluate needs a locked model (run `lock` first)", file=sys.stderr)
            return 2
        try:
            print(json.dumps(evaluate(model, splits, args.split, lock_data if args.split == "test" else None), indent=1))
        except data.SealedTestError as exc:
            print(f"SEALED: {exc}", file=sys.stderr)
            return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
