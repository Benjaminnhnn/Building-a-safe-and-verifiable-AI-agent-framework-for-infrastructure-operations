#!/usr/bin/env bash
# Phase 2 one-shot, run ONCE after lab campaign-1 has finished:
#   export -> label form -> b2 freeze -> split -> train -> lock -> propose (test snapshots) -> reproducibility check
#
#   bash evaluation/aihs/run_phase2_after_batch.sh [--with-pilot] [--allow-incomplete] [--seed N]
#                                                  [--dev-without-independent-labels]
#
# Expected stop point today: the freeze returns BLOCKED (no independent label file), the script prints why and exits 3.
# Independent labels must be written by a human other than the agent/Gate/model into
#   evaluation/aihs/.artifacts/independent-label-review.jsonl
# (start from new-a2-export/independent-label-review.template.jsonl). Labels are never derived here.
#
# --dev-without-independent-labels skips that stop: it trains/locks a DEV model on RUNNER outcomes, writes everything
# under .artifacts/phase2-dev-after-batch and marks every artifact DEV_MODEL_NOT_FROZEN_C0. Never the frozen C0.
# The test split stays sealed: only proposals are produced, no labels are opened, nothing is scored.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

PYTHON="${PYTHON:-python}"          # needs evaluation/aihs/requirements-moodle-aihs.txt (Python 3.11)
CAMPAIGN="${CAMPAIGN:-campaign-1}"
ART=evaluation/aihs/.artifacts
LAB=$ART/lab-campaign
DEV=0; WITH_PILOT=0; ALLOW_INCOMPLETE=0; SEED=20261115
while [ $# -gt 0 ]; do
  case "$1" in
    --dev-without-independent-labels) DEV=1 ;;
    --with-pilot) WITH_PILOT=1 ;;
    --allow-incomplete) ALLOW_INCOMPLETE=1 ;;
    --seed) SEED="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

echo "== 0. completeness of $CAMPAIGN"
read -r DONE PLANNED < <("$PYTHON" - "$LAB/$CAMPAIGN" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
planned = int(json.loads((root / "plan.json").read_text())["episodes"])
done = sum(1 for d in (root / "episodes").glob("*") if (d / "manifest.json").is_file())
print(done, planned)
PY
)
echo "   episodes finished: $DONE / $PLANNED"
if [ "$DONE" -lt "$PLANNED" ] && [ "$ALLOW_INCOMPLETE" -eq 0 ]; then
  echo "STOP: campaign is not finished ($DONE/$PLANNED). Wait for the batch (bash lab/moodle-local/lab.sh progress) or pass --allow-incomplete for a DEV run." >&2
  exit 2
fi
if [ "$DONE" -lt "$PLANNED" ] && [ "$DEV" -eq 0 ]; then
  echo "STOP: an incomplete campaign can only be used with --dev-without-independent-labels." >&2
  exit 2
fi

CAMPAIGN_ARGS=(--campaign "$CAMPAIGN")
[ "$WITH_PILOT" -eq 1 ] && CAMPAIGN_ARGS+=(--campaign pilot-1)
if [ "$DEV" -eq 1 ]; then EXPORT=$ART/dev-export-after-batch; OUT=$ART/phase2-dev-after-batch; else EXPORT=$ART/new-a2-export; OUT=$ART/phase2; fi
mkdir -p "$OUT"

echo "== 1. export lab records -> $EXPORT"
"$PYTHON" evaluation/aihs/a2_export.py --source lab "${CAMPAIGN_ARGS[@]}" --output-dir "$EXPORT"

echo "== 2. independent label form (EMPTY; a human must fill it)"
"$PYTHON" evaluation/aihs/lab_label_form.py --records "$EXPORT/audit-records.jsonl" --export-manifest "$EXPORT/manifest.json" --out-dir "$EXPORT"

echo "== 3. freeze (b2_freeze_dataset.py, --purge-overlap embargo for parallel workers)"
LABELS=$ART/independent-label-review.jsonl
FREEZE_ARGS=(--records "$EXPORT/audit-records.jsonl" --source-manifest "$EXPORT/manifest.json" --allowed-mode lab
             --manifest-out "$OUT/freeze-manifest.json" --output-root "$ART/b2-freeze" --purge-overlap)
[ -f "$LABELS" ] && FREEZE_ARGS+=(--label-review "$LABELS")
"$PYTHON" evaluation/aihs/b2_freeze_dataset.py "${FREEZE_ARGS[@]}" > "$OUT/freeze-result.json" || true
STATUS=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))['status'])" "$OUT/freeze-manifest.json")
echo "   freeze status: $STATUS"
if [ "$STATUS" != "READY" ]; then
  "$PYTHON" -c "import json,sys; m=json.load(open(sys.argv[1])); print('   reasons:', m['reasons']); print('   excluded:', m['excluded'])" "$OUT/freeze-manifest.json"
  if [ "$DEV" -eq 0 ]; then
    echo "STOP (expected until labels exist): freeze is $STATUS. Provide $LABELS (independent human labels) and re-run." >&2
    echo "      Thresholds are NOT lowered and no labels are fabricated. Use --dev-without-independent-labels for a DEV model." >&2
    exit 3
  fi
  echo "   DEV mode: continuing WITHOUT a frozen dataset; the result is DEV_MODEL_NOT_FROZEN_C0."
fi

echo "== 4. split"
if [ "$STATUS" = "READY" ] && [ "$DEV" -eq 0 ]; then
  "$PYTHON" evaluation/aihs/train_moodle_aihs.py split --records "$EXPORT/audit-records.jsonl" --out-dir "$OUT" --freeze-manifest "$OUT/freeze-manifest.json"
else
  "$PYTHON" evaluation/aihs/train_moodle_aihs.py split --records "$EXPORT/audit-records.jsonl" --out-dir "$OUT" --dev-without-independent-labels
fi

COMMON=(--records "$EXPORT/audit-records.jsonl" --split-manifest "$OUT/split-manifest.json")
for VARIANT in paper as-is; do   # as-is is the reference-only prototype behaviour; its results are never mixed with paper
  echo "== 5. train $VARIANT (K chosen on validation only; test never scored)"
  "$PYTHON" evaluation/aihs/train_moodle_aihs.py train "${COMMON[@]}" --baseline "$VARIANT" --seed "$SEED" --out-dir "$OUT/$VARIANT"
  echo "== 6. lock $VARIANT"
  "$PYTHON" evaluation/aihs/train_moodle_aihs.py lock "${COMMON[@]}" --model-dir "$OUT/$VARIANT"
  echo "== 7. propose on test snapshots ($VARIANT) - proposals only"
  "$PYTHON" evaluation/aihs/train_moodle_aihs.py propose "${COMMON[@]}" --model-dir "$OUT/$VARIANT"
done
echo "== 8. reproducibility check (retrain from scratch; compare model hashes and proposals)"
"$PYTHON" evaluation/aihs/train_moodle_aihs.py repro-check "${COMMON[@]}" --model-dir "$OUT/paper"
echo "DONE. Outputs: $OUT"
if [ "$DEV" -eq 1 ]; then echo "NOTE: DEV_MODEL_NOT_FROZEN_C0 - runner-outcome labels, not independent, not frozen."; fi
