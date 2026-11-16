"""Mapper step 1: snapshot features -> AIHS-style attribute documents.

The paper/prototype Mapper discretizes every attribute into six severity levels (info, good, warning, error,
critical, fatal = 0..5) and builds a document in which each attribute name is repeated ``level`` times. Moodle
snapshot features have no a-priori severity scale, so the six ordinal levels are fitted per attribute on the TRAIN
split only (quantile edges); level 0 means "at or below the typical-low train value" and emits no word. This is a
documented deviation (data-driven ordinal level, not an operator-assigned severity).

Only ``pre_decision.features`` may enter here. Fields of the audit-only ``episode`` block, ``scenario_code``,
outcomes and receipts are leakage and are rejected by name.
"""

from __future__ import annotations

import hashlib
import json
import re

import numpy as np

LEVELS = 6                      # info..fatal -> 0..5
FEATURE_SPEC_VERSION = "moodle-snapshot-attribute-documents-v1"
TOKEN_PATTERN = r"\S+"
# Same answer-bearing name rule as a2_export_lab.py (superset of b2_freeze_dataset.py).
FORBIDDEN_FEATURE = re.compile(
    r"scenario|ground.?truth|outcome|verifier|gate|approval|receipt|action|incident|run.?id|fault|answer|label|post.?decision"
    r"|signature|idempot|episode|marker|injected|reset|baseline|variant|transient|exploration|behavior|policy",
    re.IGNORECASE)
# Keys that must never be present inside a feature mapping at all (audit-only block names).
FORBIDDEN_BLOCKS = ("episode", "scenario_code", "plan", "outcome", "post_decision", "proposal")


def feature_spec() -> dict:
    return {"version": FEATURE_SPEC_VERSION, "levels": LEVELS, "document_rule": "attribute name repeated `level` times",
            "discretization": "train-only quantile edges at i/6 (i=1..5), unique, level = #edges strictly below value",
            "missing_value": "no word emitted", "token_pattern": TOKEN_PATTERN, "lowercase": False,
            "forbidden_feature_regex": FORBIDDEN_FEATURE.pattern}


def feature_spec_sha256() -> str:
    return hashlib.sha256(json.dumps(feature_spec(), sort_keys=True).encode()).hexdigest()


def check_feature_names(features: dict) -> None:
    for name in features:
        if name in FORBIDDEN_BLOCKS or FORBIDDEN_FEATURE.search(name):
            raise ValueError(f"leakage: answer-bearing or post-decision feature name {name!r}")


class AttributeDiscretizer:
    """Train-only fit of per-attribute quantile edges."""

    def __init__(self):
        self.edges: dict[str, list[float]] = {}
        self.fitted = False

    def fit(self, train_features: list[dict]) -> "AttributeDiscretizer":
        names = sorted({k for f in train_features for k in f})
        for f in train_features:
            check_feature_names(f)
        edges = {}
        for name in names:
            values = np.array([f[name] for f in train_features if f.get(name) is not None], dtype=float)
            if values.size == 0:
                edges[name] = []
                continue
            quantiles = np.quantile(values, [i / LEVELS for i in range(1, LEVELS)])
            edges[name] = sorted({float(round(q, 12)) for q in quantiles})
        self.edges, self.fitted = edges, True
        return self

    def levels(self, features: dict) -> dict[str, int]:
        if not self.fitted:
            raise RuntimeError("discretizer is not fitted")
        check_feature_names(features)
        result = {}
        for name, edge_list in self.edges.items():
            value = features.get(name)
            if value is None:
                continue
            result[name] = int(np.searchsorted(np.array(edge_list), float(value), side="left")) if edge_list else 0
        return result

    def document(self, features: dict) -> str:
        words: list[str] = []
        for name, level in self.levels(features).items():
            words.extend([name] * level)
        return " ".join(words)

    def state(self) -> dict:
        return {"edges": self.edges}
