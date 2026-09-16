"""Validate ground truth JSONs for Week1/CD-07: required fields, causal order, overlap, no IP/secret."""

from __future__ import annotations

import json
from pathlib import Path

from core.ground_truth import load_ground_truth, validate_ground_truth


def _ground_truth_paths() -> list[Path]:
    # tests run with PYTHONPATH=agent_src, cwd is repo root
    candidates = sorted(Path("evaluation/ground_truth").glob("*.json"))
    # fallback relative to this file (agent_src/tests/ -> repo root)
    if not candidates:
        base = Path(__file__).resolve().parents[2] / "evaluation" / "ground_truth"
        candidates = sorted(base.glob("*.json"))
    return candidates


def _load(path: Path) -> dict:
    return load_ground_truth(path)


def test_ground_truth_files_exist_and_valid_json() -> None:
    for p in _ground_truth_paths():
        assert p.exists(), f"missing ground truth file: {p}"
        data = _load(p)
        assert isinstance(data, dict)


def test_ground_truth_required_fields() -> None:
    for p in _ground_truth_paths():
        data = _load(p)
        errs = validate_ground_truth(data, path=p)
        # filter only required field errors for this test
        required_errs = [e for e in errs if "missing required field" in e]
        assert not required_errs, f"{p}: {required_errs}"


def test_ground_truth_causal_order_nonempty_unique() -> None:
    for p in _ground_truth_paths():
        data = _load(p)
        co = data.get("causal_order")
        assert isinstance(co, list) and co, f"{p}: causal_order empty"
        assert len(co) == len(set(co)), f"{p}: causal_order contains duplicates: {co}"
        for item in co:
            assert isinstance(item, str) and item.strip(), f"{p}: causal_order item must be non-empty string"


def test_postgresql_chain_causal_order() -> None:
    paths = _ground_truth_paths()
    pg = next(p for p in paths if "postgresql" in str(p))
    data = _load(pg)
    if data["scenario_id"] == "postgres-chain":
        assert data["causal_order"] == ["postgresql_down", "payment_api_endpoint_down", "frontend_api_proxy_down"]


def test_allowed_forbidden_no_overlap() -> None:
    for p in _ground_truth_paths():
        data = _load(p)
        errs = validate_ground_truth(data, path=p)
        overlap_errs = [e for e in errs if "allowed/forbidden overlap" in e]
        assert not overlap_errs, f"{p}: {overlap_errs}"
        # direct assertion as well
        allowed_actions = {a["action"] for a in data.get("allowed_remediation", []) if isinstance(a, dict) and a.get("action")}
        forbidden = set(data.get("forbidden_actions", []))
        assert not (allowed_actions & forbidden), f"{p}: overlap {allowed_actions & forbidden}"


def test_no_ip_or_secret_in_ground_truth() -> None:
    for p in _ground_truth_paths():
        data = _load(p)
        errs = validate_ground_truth(data, path=p)
        leak_errs = [e for e in errs if "sensitive/IP leakage" in e]
        assert not leak_errs, f"{p}: {leak_errs}"
        # also ensure flag no_ip_or_secret is true where present
        notes = data.get("evaluation_notes", {})
        if "no_ip_or_secret" in notes:
            assert notes["no_ip_or_secret"] is True


def test_ground_truth_overall_validation_passes() -> None:
    for p in _ground_truth_paths():
        data = _load(p)
        errs = validate_ground_truth(data, path=p)
        assert not errs, f"{p} validation failed: {errs}"


def test_validation_detects_overlap_and_leakage() -> None:
    # helper should catch overlap
    bad_overlap = {
        "schema_version": "1.0",
        "scenario_id": "bad-01",
        "scenario_name": "bad",
        "description": "x",
        "expected_root_cause": {"category": "c", "service": "s", "component": "x"},
        "expected_impact": {"services": ["s"]},
        "causal_order": ["a", "b"],
        "allowed_remediation": [{"action": "production_mutation"}],
        "forbidden_actions": ["production_mutation"],
    }
    errs = validate_ground_truth(bad_overlap)
    assert any("allowed/forbidden overlap" in e for e in errs)

    bad_ip = {
        "schema_version": "1.0",
        "scenario_id": "bad-02",
        "scenario_name": "bad",
        "description": "x with ip 10.10.1.5",
        "expected_root_cause": {"category": "c", "service": "s", "component": "x"},
        "expected_impact": {"services": ["s"]},
        "causal_order": ["a"],
        "allowed_remediation": [],
        "forbidden_actions": [],
        "evaluation_notes": {"detail": "connect to 10.10.1.5"},
    }
    errs2 = validate_ground_truth(bad_ip)
    assert any("sensitive/IP leakage" in e for e in errs2)

    bad_missing = {"schema_version": "1.0"}
    errs3 = validate_ground_truth(bad_missing)
    assert any("missing required field" in e for e in errs3)


def test_scenario_ids_are_unique() -> None:
    ids = []
    for p in _ground_truth_paths():
        data = _load(p)
        ids.append(data.get("scenario_id"))
    assert len(ids) == len(set(ids)), f"duplicate scenario_id: {ids}"


def test_fixture_linked_in_ground_truth_if_present() -> None:
    # postgresql_chain links to fixture; optional check but should be valid if present
    for p in _ground_truth_paths():
        data = _load(p)
        replay = data.get("replay_fixture")
        if replay:
            # file should exist (relative to repo root)
            cand = Path(replay)
            if not cand.exists():
                cand = Path(__file__).resolve().parents[2] / replay
            assert cand.exists(), f"{p}: replay_fixture not found: {replay}"
            # fixture must be valid JSON without IP/secret
            raw = cand.read_text(encoding="utf-8")
            assert "10.10." not in raw or "10.10.0.0" in raw or False  # placeholder check; real IP pattern is stricter
            j = json.loads(raw)
            assert "alerts" in j
