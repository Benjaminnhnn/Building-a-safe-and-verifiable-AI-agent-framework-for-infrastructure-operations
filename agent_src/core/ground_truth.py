"""Helper to load and validate ground truth JSONs under evaluation/ground_truth.

Reusable by tests and future benchmark scripts. No raw IP/secret values are allowed
in ground truth files; validation enforces required fields, causal order,
and allowed/forbidden non-overlap.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Required top-level keys for every ground truth file.
REQUIRED_TOP_FIELDS = {
    "schema_version",
    "scenario_id",
    "scenario_name",
    "description",
    "expected_root_cause",
    "expected_impact",
    "causal_order",
    "allowed_remediation",
    "forbidden_actions",
}

REQUIRED_ROOT_CAUSE_FIELDS = {"category", "service", "component"}
REQUIRED_IMPACT_FIELDS = {"services"}

# Reuse sensitive patterns from event_schema but keep helper self-contained.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|aws[_-]?secret|credential|auth|ssn|credit[_-]?card|card[_-]?number|pii)",
    re.IGNORECASE,
)

SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
    re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
]

# IP detection: strict IPv4 to avoid flagging version numbers like "1.0"
IP_PATTERN = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(?:25[0-5]|2[0-4]\d|1?\d{1,2})\b")


def _scan_for_sensitive(obj: Any, path: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = f"{path}.{k}" if path else str(k)
            if SENSITIVE_KEY_PATTERN.search(str(k)):
                findings.append(f"{cur}: sensitive key")
            if isinstance(v, str):
                if any(p.search(v) for p in SENSITIVE_VALUE_PATTERNS):
                    findings.append(f"{cur}: sensitive value pattern")
                if IP_PATTERN.search(v):
                    # allow localhost/placeholder; flag only if looks like real private/public IP
                    # ground truth must not contain any IP per spec
                    findings.append(f"{cur}: ip address")
            findings.extend(_scan_for_sensitive(v, cur))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            findings.extend(_scan_for_sensitive(item, f"{path}[{idx}]"))
    elif isinstance(obj, str):
        if any(p.search(obj) for p in SENSITIVE_VALUE_PATTERNS):
            findings.append(f"{path}: sensitive value pattern")
        if IP_PATTERN.search(obj):
            findings.append(f"{path}: ip address")
    return findings


def load_ground_truth(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"ground truth must be an object: {p}")
    return data


def validate_ground_truth(data: dict[str, Any], *, path: str | Path | None = None) -> list[str]:
    """Return list of error strings; empty means valid."""
    errors: list[str] = []
    label = str(path) if path else "ground_truth"

    for field in REQUIRED_TOP_FIELDS:
        if field not in data:
            errors.append(f"{label}: missing required field: {field}")

    # schema_version must be 1.0 or 2.0 (support both)
    sv = data.get("schema_version")
    if sv not in {"1.0", "2.0"}:
        errors.append(f"{label}: invalid schema_version: {sv}")

    # scenario_id sanity
    sid = data.get("scenario_id")
    if sid is not None and (not isinstance(sid, str) or not sid.strip()):
        errors.append(f"{label}: scenario_id must be non-empty string")

    # expected_root_cause
    erc = data.get("expected_root_cause")
    if isinstance(erc, dict):
        for f in REQUIRED_ROOT_CAUSE_FIELDS:
            if not erc.get(f):
                errors.append(f"{label}: expected_root_cause missing {f}")
    elif erc is not None:
        errors.append(f"{label}: expected_root_cause must be an object")

    # expected_impact
    ei = data.get("expected_impact")
    if isinstance(ei, dict):
        for f in REQUIRED_IMPACT_FIELDS:
            if not ei.get(f):
                errors.append(f"{label}: expected_impact missing {f}")
    elif ei is not None:
        errors.append(f"{label}: expected_impact must be an object")

    # causal_order
    co = data.get("causal_order")
    if isinstance(co, list):
        if not co:
            errors.append(f"{label}: causal_order must be non-empty")
        else:
            if len(co) != len(set(co)):
                errors.append(f"{label}: causal_order must not contain duplicates")
            for idx, item in enumerate(co):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"{label}: causal_order[{idx}] must be non-empty string")
    elif co is not None:
        errors.append(f"{label}: causal_order must be a list")

    # allowed_remediation / forbidden_actions
    allowed = data.get("allowed_remediation")
    forbidden = data.get("forbidden_actions")
    if allowed is not None and not isinstance(allowed, list):
        errors.append(f"{label}: allowed_remediation must be a list")
    if forbidden is not None and not isinstance(forbidden, list):
        errors.append(f"{label}: forbidden_actions must be a list")

    if isinstance(allowed, list) and isinstance(forbidden, list):
        # extract action names from allowed (dict action or string)
        allowed_actions: set[str] = set()
        for item in allowed:
            if isinstance(item, dict) and item.get("action"):
                allowed_actions.add(str(item["action"]))
            elif isinstance(item, str):
                allowed_actions.add(item)
        forbidden_set = {str(x) for x in forbidden}
        overlap = allowed_actions & forbidden_set
        if overlap:
            errors.append(f"{label}: allowed/forbidden overlap: {sorted(overlap)}")

    # sensitive / IP leakage across entire document
    findings = _scan_for_sensitive(data)
    if findings:
        # show first few only
        errors.append(f"{label}: sensitive/IP leakage detected: {findings[:3]}")

    return errors


def is_valid_ground_truth(data: dict[str, Any]) -> bool:
    return not validate_ground_truth(data)


def load_and_validate(path: str | Path) -> tuple[dict[str, Any], list[str]]:
    data = load_ground_truth(path)
    errs = validate_ground_truth(data, path=path)
    return data, errs


def ground_truth_dir() -> Path:
    """Return default evaluation/ground_truth dir (repo root aware)."""
    here = Path(__file__).resolve()
    # agent_src/core/ground_truth.py -> repo root is parents[2]
    repo_root = here.parents[2]
    cand = repo_root / "evaluation" / "ground_truth"
    if cand.is_dir():
        return cand
    # fallback: cwd
    return Path("evaluation/ground_truth")
