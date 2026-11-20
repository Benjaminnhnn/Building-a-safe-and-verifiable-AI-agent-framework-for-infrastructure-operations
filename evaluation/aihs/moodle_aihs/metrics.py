"""Paper-style rates plus per-class counts. Refuses the sealed test split before a lock exists.

HitRate     = share of historical SUCCESSFUL episodes whose final proposed action equals the action that worked.
ReverseRate = share of historical FAILED episodes whose final proposed action differs from the action that failed.
OverallRate = harmonic mean of the two. ``call_human`` counts as a miss on success rows and as avoidance on failure
rows (it executes nothing). Transient (self-healing) episodes are excluded: their success/failure is confounded.
In the lab, "success/failure" is the RUNNER outcome, not an independent label.
"""

from __future__ import annotations

from collections import Counter

from .data import SealedTestError, Splits


def _rate(num: int, den: int):
    return None if den == 0 else num / den


def harmonic(a, b):
    if a is None or b is None:
        return None
    return 0.0 if (a + b) == 0 else 2 * a * b / (a + b)


def rates(rows: list[dict], finals: list[str], predicted: list[str] | None = None) -> dict:
    use = [(r, f, (predicted or finals)[i]) for i, (r, f) in enumerate(zip(rows, finals)) if not r["transient"]]
    succ = [(r, f, p) for r, f, p in use if r["outcome"] == "success"]
    fail = [(r, f, p) for r, f, p in use if r["outcome"] == "failure"]
    hit = _rate(sum(f == r["action"] for r, f, _ in succ), len(succ))
    rev = _rate(sum(f != r["action"] for r, f, _ in fail), len(fail))
    pred_hit = _rate(sum(p == r["action"] for r, _, p in succ), len(succ))
    pred_rev = _rate(sum(p != r["action"] for r, _, p in fail), len(fail))
    return {
        "rows": len(rows), "transient_excluded": len(rows) - len(use),
        "success_rows": len(succ), "failure_rows": len(fail),
        "HitRate": hit, "ReverseRate": rev, "OverallRate": harmonic(hit, rev),
        "predictor_only": {"HitRate": pred_hit, "ReverseRate": pred_rev, "OverallRate": harmonic(pred_hit, pred_rev)},
        "call_human_share": _rate(sum(f == "call_human" for _, f, _ in use), len(use)),
        "per_class_counts": {
            "executed_action_x_outcome": {f"{a}|{o}": n for (a, o), n in sorted(Counter((r["action"], r["outcome"]) for r, _, _ in use).items())},
            "final_proposed_action": dict(sorted(Counter(f for _, f, _ in use).items())),
        },
    }


def evaluate(model, splits: Splits, name: str, lock: dict | None = None) -> dict:
    """Score ``model`` on train or validation. The test split needs a lock that matches this model."""
    if name == "test":
        if not lock or lock.get("model_content_sha256") != model.content_digest():
            raise SealedTestError("refusing to score test: the model is not locked (or does not match the lock)")
    rows = splits.rows(name, lock)
    proposals = [model.propose(r["features"]) for r in rows]
    result = rates(rows, [p["proposed_action"] for p in proposals], [p["predictor_action"] for p in proposals])
    result["fallback_paths"] = dict(sorted(Counter(p["path"] for p in proposals).items()))
    return result


def calibration_note(model, splits: Splits, name: str = "validation") -> dict:
    """Controller reliability on train/validation executed rows: mean predicted P(success) vs observed, Brier."""
    import numpy as np
    out = {}
    rows = [r for r in splits.rows(name) if r["outcome"] in ("success", "failure") and not r["transient"]]
    for action, ctl in sorted(model.controllers.items()):
        sel = [r for r in rows if r["action"] == action]
        if not sel:
            out[action] = {"n": 0}
            continue
        topics = model.mapper.transform([r["features"] for r in sel])
        classes = list(ctl.classes_)
        p = ctl.predict_proba(topics)[:, classes.index(1)] if 1 in classes else np.zeros(len(sel))
        y = np.array([1.0 if r["outcome"] == "success" else 0.0 for r in sel])
        out[action] = {"n": len(sel), "mean_predicted_p_success": float(p.mean()), "observed_success_rate": float(y.mean()),
                       "brier": float(((p - y) ** 2).mean())}
    out["note"] = ("Per-action controller reliability on small samples; only indicative. Probabilities are "
                   "uncalibrated LightGBM outputs with class_weight=balanced (no Platt/isotonic step).")
    return out
