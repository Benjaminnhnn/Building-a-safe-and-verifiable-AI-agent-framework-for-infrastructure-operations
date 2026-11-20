"""Predictor (LightGBM multiclass), Controllers (one LightGBM binary per action) and the two baseline behaviours.

paper  (`aihs-paper-repro-moodle-v1`): Predictor on successful executed non-transient episodes only; Controller
        fallback tries the remaining actions in ascending ladder cost with each controller's ``.predict``, else
        ``call_human``.
as_is  (`aihs-public-prototype-as-is-v1`): Predictor on ALL executed rows, fixed K, and the prototype's fallback
        bug (a rejected proposal goes straight to ``call_human``; no alternative classifier is ever called).
        Reference only, never mixed with the paper variant in a result.
"""

from __future__ import annotations

import hashlib
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_ladder import CALL_HUMAN, COST_LADDER, PREDICTOR_CLASSES, spec  # noqa: E402

from . import AS_IS_BASELINE_ID, PAPER_BASELINE_ID  # noqa: E402
from .mapper import Mapper  # noqa: E402

MIN_CONTROLLER_PER_CLASS = 2   # need >=2 successes AND >=2 failures of an action to learn its controller


def lgbm_params(seed: int) -> dict:
    return dict(n_estimators=200, learning_rate=0.05, num_leaves=8, min_child_samples=2, reg_lambda=1.0,
                class_weight="balanced", random_state=seed, deterministic=True, force_row_wise=True,
                n_jobs=1, verbose=-1)


class ConstantClassifier:
    """Used when only one class is observed; LightGBM cannot fit a single-class problem."""

    def __init__(self, label):
        self.label = label
        self.classes_ = np.array([label])

    def predict(self, x):
        return np.array([self.label] * len(x))

    def predict_proba(self, x):
        return np.ones((len(x), 1))


def _lgbm(seed):
    from lightgbm import LGBMClassifier
    return LGBMClassifier(**lgbm_params(seed))


class MoodleAIHSModel:
    def __init__(self, baseline_id: str, n_topics: int, seed: int):
        if baseline_id not in (PAPER_BASELINE_ID, AS_IS_BASELINE_ID):
            raise ValueError(f"unknown baseline id {baseline_id!r}")
        self.baseline_id, self.n_topics, self.seed = baseline_id, n_topics, seed
        self.mapper = Mapper(n_topics, seed)
        self.predictor = None
        self.predictor_classes: list[str] = []
        self.controllers: dict[str, object] = {}
        self.coverage: dict[str, dict] = {}
        self.limitations: list[str] = []
        self.counts: dict = {}

    @property
    def variant(self) -> str:
        return "paper" if self.baseline_id == PAPER_BASELINE_ID else "as_is"

    # ------------------------------------------------------------------ fit
    def fit(self, train: list[dict]) -> "MoodleAIHSModel":
        """``train``: dicts with features, action (executed action), outcome (success|failure), transient (bool).

        The Mapper (vocabulary + LDA) uses every TRAIN row; Predictor/Controllers use the subsets below."""
        self.mapper.fit([r["features"] for r in train])
        topics = self.mapper.transform([r["features"] for r in train])
        executed = [i for i, r in enumerate(train) if r["outcome"] in ("success", "failure")]
        if self.variant == "paper":
            predictor_rows = [i for i in executed if train[i]["outcome"] == "success" and not train[i]["transient"]]
            controller_pool = [i for i in executed if not train[i]["transient"]]
        else:
            predictor_rows, controller_pool = executed, executed
        for i in executed:
            spec(train[i]["action"])                                   # fail closed on foreign actions
        labels = [train[i]["action"] for i in predictor_rows]
        self.counts = {"train_rows": len(train), "predictor_rows": len(predictor_rows),
                       "predictor_class_counts": dict(sorted(Counter(labels).items())),
                       "transient_excluded": sum(1 for i in executed if train[i]["transient"]) if self.variant == "paper" else 0}
        if not predictor_rows:
            raise ValueError("no usable Predictor training rows (need executed successful non-transient episodes)")
        if len(set(labels)) < 2:
            self.predictor = ConstantClassifier(labels[0])
            self.limitations.append(f"predictor_single_class:{labels[0]}")
        else:
            self.predictor = _lgbm(self.seed).fit(topics[predictor_rows], labels)
        self.predictor_classes = sorted(set(labels))
        for action in PREDICTOR_CLASSES:
            ok = [i for i in controller_pool if train[i]["action"] == action]
            n_s = sum(train[i]["outcome"] == "success" for i in ok)
            n_f = len(ok) - n_s
            trained, reason = False, None
            if n_s >= MIN_CONTROLLER_PER_CLASS and n_f >= MIN_CONTROLLER_PER_CLASS:
                y = np.array([1 if train[i]["outcome"] == "success" else 0 for i in ok])
                self.controllers[action] = _lgbm(self.seed).fit(topics[ok], y)
                trained = True
            elif action == CALL_HUMAN:
                reason = "terminal_action_never_executed"
            else:
                reason = (f"needs>={MIN_CONTROLLER_PER_CLASS}_success_and_{MIN_CONTROLLER_PER_CLASS}_failure_executed_rows"
                          f"(have success={n_s},failure={n_f})")
                self.limitations.append(f"controller_missing:{action}:{reason}")
            self.coverage[action] = {"executed_success": n_s, "executed_failure": n_f, "controller": trained,
                                     "reason_missing": reason}
        return self

    # -------------------------------------------------------------- inference
    def _controller_verdict(self, action: str, topic_row: np.ndarray) -> dict:
        ctl = self.controllers.get(action)
        if ctl is None:
            return {"action": action, "controller": "absent", "predicted_success": None, "p_success": None}
        pred = int(ctl.predict(topic_row)[0])
        classes = list(ctl.classes_)
        p1 = float(ctl.predict_proba(topic_row)[0][classes.index(1)]) if 1 in classes else None
        return {"action": action, "controller": "present", "predicted_success": pred == 1, "p_success": p1}

    def propose(self, features: dict) -> dict:
        """Return predictor_action, proposed_action, controller_trace, path. Untrusted proposal only."""
        topic = self.mapper.transform([features])
        predicted = str(self.predictor.predict(topic)[0])
        spec(predicted)
        trace = [self._controller_verdict(predicted, topic)]
        first = trace[0]
        if predicted == CALL_HUMAN:
            return self._result(predicted, CALL_HUMAN, trace, "predictor_call_human")
        if first["controller"] == "absent":
            return self._result(predicted, predicted, trace, "no_controller_for_proposal_unvetted")
        if first["predicted_success"]:
            return self._result(predicted, predicted, trace, "controller_accepted")
        if self.variant == "as_is":
            # Prototype bug reproduced: classifier objects are compared with 1, so no alternative is evaluated.
            return self._result(predicted, CALL_HUMAN, trace, "prototype_fallback_bug_call_human")
        for action in COST_LADDER:                                       # ascending cost; ladder order breaks ties
            if action in (predicted, CALL_HUMAN):
                continue
            verdict = self._controller_verdict(action, topic)
            trace.append(verdict)
            if verdict["controller"] == "present" and verdict["predicted_success"]:
                return self._result(predicted, action, trace, "fallback_cheapest_accepted")
        return self._result(predicted, CALL_HUMAN, trace, "fallback_exhausted_call_human")

    @staticmethod
    def _result(predicted, proposed, trace, path):
        return {"predictor_action": predicted, "proposed_action": proposed, "controller_trace": trace, "path": path}

    def content_digest(self) -> str:
        h = hashlib.sha256()
        h.update(self.baseline_id.encode())
        h.update(self.mapper.content_digest().encode())
        for name, obj in [("predictor", self.predictor)] + sorted(self.controllers.items()):
            h.update(str(name).encode())
            booster = getattr(obj, "booster_", None)
            h.update((booster.model_to_string() if booster is not None else repr(obj.label)).encode())
        return h.hexdigest()
