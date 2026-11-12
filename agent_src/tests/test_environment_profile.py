from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.environment_profile import (
    DEFAULT_PROFILE_PATH,
    EnvironmentProfileError,
    load_environment_profile,
)

PRODUCTION_EXAMPLE = DEFAULT_PROFILE_PATH.parent / "production.example.json"


def _write(tmp_path: Path, data: dict) -> tuple[Path, str]:
    path = tmp_path / "profile.json"
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _reviewed_production() -> dict:
    data = json.loads(PRODUCTION_EXAMPLE.read_text(encoding="utf-8"))
    data.update(reviewed=True, reviewed_by="safety-lead")
    return data


def test_default_staging_profile_reproduces_previous_constants() -> None:
    profile = load_environment_profile(DEFAULT_PROFILE_PATH, pinned_sha256="")
    assert profile.environment == "staging"
    assert profile.node_hosts() == ("moodle-app-a", "moodle-app-b")
    assert profile.executor_nodes("staging_moodle_nodes") == {"a", "b"}
    assert profile.executor_nodes("moodle-app-b") == {"b"}
    assert profile.cron_node().host == "moodle-app-a"
    assert profile.gate.min_confidence == 0.65 and profile.gate.min_evidence_refs == 3
    assert profile.url_allowed("http://moodle-staging-alb-1.eu-west-1.elb.amazonaws.com/")
    assert not profile.url_allowed("https://moodle.example.edu/")
    assert len(profile.sha256) == 64


def test_unknown_scope_has_no_nodes() -> None:
    profile = load_environment_profile(DEFAULT_PROFILE_PATH, pinned_sha256="")
    assert profile.executor_nodes("does-not-exist") == frozenset()


def test_shipped_production_example_is_refused() -> None:
    with pytest.raises(EnvironmentProfileError, match="reviewed"):
        load_environment_profile(PRODUCTION_EXAMPLE, pinned_sha256="")


def test_reviewed_production_requires_matching_pin(tmp_path: Path) -> None:
    path, digest = _write(tmp_path, _reviewed_production())
    with pytest.raises(EnvironmentProfileError, match="pinned"):
        load_environment_profile(path, pinned_sha256="")
    with pytest.raises(EnvironmentProfileError, match="does not match"):
        load_environment_profile(path, pinned_sha256="0" * 64)
    profile = load_environment_profile(path, pinned_sha256=digest)
    assert profile.environment == "production" and profile.sha256 == digest


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"approval": {"mode": "operator", "approved_actors": ["x"], "ttl_max_seconds": 600}}, "two_person"),
        ({"executor": {"allow_plain_http": True}}, "plain-HTTP"),
        ({"gate": {"min_confidence": 0.8, "auto_confidence": 0.9, "min_evidence_refs": 3}}, "every mutation"),
    ],
)
def test_production_safety_floor_cannot_be_lowered(tmp_path: Path, change: dict, message: str) -> None:
    path, digest = _write(tmp_path, {**_reviewed_production(), **change})
    with pytest.raises(EnvironmentProfileError, match=message):
        load_environment_profile(path, pinned_sha256=digest)


@pytest.mark.parametrize(
    "change",
    [
        {"environment": "prod"},
        {"unexpected": True},
        {"executor_scopes": {"x": ["z"]}},
        {"public_url_patterns": ["moodle"]},
        {"gate": {"min_confidence": 0.4, "auto_confidence": 0.8, "min_evidence_refs": 3}},
        {"sha256": "a" * 64},
        {"reviewed": False},
    ],
)
def test_malformed_or_unreviewed_profiles_fail_closed(tmp_path: Path, change: dict) -> None:
    data = json.loads(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    path, _ = _write(tmp_path, {**data, **change})
    with pytest.raises(EnvironmentProfileError):
        load_environment_profile(path, pinned_sha256="")


def test_staging_pin_is_enforced_when_set(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentProfileError, match="does not match"):
        load_environment_profile(DEFAULT_PROFILE_PATH, pinned_sha256="f" * 64)
