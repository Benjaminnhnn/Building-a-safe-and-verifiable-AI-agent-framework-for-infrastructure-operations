"""Reviewed environment profile: the single source of environment-specific values.

Staging guards used to be string literals ("staging", "moodle-app-b", 0.65 ...)
spread over many modules. They now read one declarative profile instead. The
guards themselves stay: a profile can only *describe* an environment, and a
non-staging profile is refused unless it is explicitly reviewed and its exact
bytes are pinned by SHA-256 out of band.

Rules (all fail closed, raising ``EnvironmentProfileError``):
- unknown fields, unknown environment names or malformed values are rejected;
- ``reviewed`` must be true and ``reviewed_by`` set;
- a non-staging profile must be pinned via ``AIOPS_ENVIRONMENT_PROFILE_SHA256``
  and must use two-person approval, no plain-HTTP executor and
  ``auto_confidence`` of 1.0 (every mutation needs approval);
- every executor scope may only name declared node aliases.

With no configuration the bundled staging profile is loaded, which reproduces
the previous hard-coded behaviour exactly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROFILE_PATH_ENV = "AIOPS_ENVIRONMENT_PROFILE"
PROFILE_SHA256_ENV = "AIOPS_ENVIRONMENT_PROFILE_SHA256"
_PROFILE_DIR = Path(__file__).resolve().parents[1] / "config" / "environment_profiles"
DEFAULT_PROFILE_PATH = _PROFILE_DIR / "staging.json"


class EnvironmentProfileError(RuntimeError):
    """The environment profile is missing, invalid, unreviewed or unpinned."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeProfile(_Strict):
    alias: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    host: str = Field(pattern=r"^[A-Za-z0-9.-]{1,253}$")
    roles: tuple[Literal["web", "cron"], ...] = Field(min_length=1)


class ContainerProfile(_Strict):
    web: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    cron: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")


class ApprovalProfile(_Strict):
    mode: Literal["operator", "two_person"]
    approved_actors: tuple[str, ...] = Field(min_length=1)
    ttl_max_seconds: int = Field(ge=1, le=900)


class GateProfile(_Strict):
    min_confidence: float = Field(ge=0.5, le=1.0)
    auto_confidence: float = Field(ge=0.5, le=1.0)
    min_evidence_refs: int = Field(ge=3, le=20)

    @model_validator(mode="after")
    def _ordered(self) -> "GateProfile":
        if self.auto_confidence < self.min_confidence:
            raise ValueError("auto_confidence must not be below min_confidence")
        return self


class VerificationProfile(_Strict):
    stability_window_seconds: int = Field(ge=60, le=3600)
    min_samples: int = Field(ge=3, le=100)
    max_attestation_age_seconds: int = Field(ge=30, le=600)
    max_future_skew_seconds: int = Field(ge=0, le=60)


class EvidenceProfile(_Strict):
    max_sample_age_seconds: int = Field(ge=15, le=600)


class ExecutorProfile(_Strict):
    allow_plain_http: bool


class EnvironmentProfile(_Strict):
    schema_version: Literal[1]
    environment: Literal["staging", "production"]
    reviewed: bool
    reviewed_by: str
    reviewed_at: str
    description: str = ""
    public_url_patterns: tuple[str, ...] = Field(min_length=1)
    monitor_host: str = Field(pattern=r"^[A-Za-z0-9.-]{1,253}$")
    nodes: tuple[NodeProfile, ...] = Field(min_length=1)
    executor_scopes: dict[str, tuple[str, ...]] = Field(min_length=1)
    containers: ContainerProfile
    approval: ApprovalProfile
    gate: GateProfile
    verification: VerificationProfile
    evidence: EvidenceProfile
    executor: ExecutorProfile
    sha256: str = ""

    @field_validator("public_url_patterns")
    @classmethod
    def _anchored_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            if not (pattern.startswith("^") and pattern.endswith("$")):
                raise ValueError("public URL patterns must be anchored with ^...$")
            re.compile(pattern)
        return patterns

    @model_validator(mode="after")
    def _consistent(self) -> "EnvironmentProfile":
        aliases = [node.alias for node in self.nodes]
        hosts = [node.host for node in self.nodes]
        if len(set(aliases)) != len(aliases) or len(set(hosts)) != len(hosts):
            raise ValueError("node aliases and hosts must be unique")
        for scope, members in self.executor_scopes.items():
            if not members or not set(members) <= set(aliases):
                raise ValueError(f"executor scope {scope!r} names undeclared or no nodes")
        if sum("cron" in node.roles for node in self.nodes) != 1:
            raise ValueError("exactly one node must carry the cron role")
        if not self.reviewed or not self.reviewed_by.strip():
            raise ValueError("profile is not marked reviewed with a named reviewer")
        if self.environment != "staging":
            if self.approval.mode != "two_person":
                raise ValueError("non-staging profiles require two_person approval")
            if self.executor.allow_plain_http:
                raise ValueError("non-staging profiles must not allow a plain-HTTP executor")
            if self.gate.auto_confidence < 1.0:
                raise ValueError("non-staging profiles must require approval for every mutation")
        return self

    # --- convenience accessors used by the guards -------------------------
    def node_hosts(self) -> tuple[str, ...]:
        return tuple(node.host for node in self.nodes)

    def host_for(self, alias: str) -> str:
        for node in self.nodes:
            if node.alias == alias:
                return node.host
        raise KeyError(alias)

    def cron_node(self) -> NodeProfile:
        return next(node for node in self.nodes if "cron" in node.roles)

    def executor_nodes(self, scope: str) -> frozenset[str]:
        return frozenset(self.executor_scopes.get(scope, ()))

    def url_allowed(self, url: str) -> bool:
        return any(re.match(pattern, url) for pattern in self.public_url_patterns)

    def audit_reference(self) -> dict[str, str]:
        return {"environment": self.environment, "profile_sha256": self.sha256}


def _canonical_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_environment_profile(path: str | Path | None = None, *, pinned_sha256: str | None = None) -> EnvironmentProfile:
    """Load and validate a profile; raise ``EnvironmentProfileError`` on any doubt."""
    profile_path = Path(path or os.getenv(PROFILE_PATH_ENV) or DEFAULT_PROFILE_PATH)
    try:
        raw = profile_path.read_bytes()
    except OSError as exc:
        raise EnvironmentProfileError(f"environment profile is unreadable: {profile_path}") from exc
    digest = _canonical_sha256(raw)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or "sha256" in data:
            raise ValueError("profile must be a JSON object without a self-declared sha256")
        profile = EnvironmentProfile.model_validate({**data, "sha256": digest})
    except (ValueError, ValidationError) as exc:
        raise EnvironmentProfileError(f"environment profile is invalid: {exc}") from exc

    pin = pinned_sha256 if pinned_sha256 is not None else os.getenv(PROFILE_SHA256_ENV, "")
    pin = pin.strip().lower()
    if profile.environment != "staging" and not pin:
        raise EnvironmentProfileError("non-staging profile requires a pinned SHA-256")
    if pin and pin != digest:
        raise EnvironmentProfileError("environment profile SHA-256 does not match the pinned value")
    return profile


@lru_cache(maxsize=1)
def get_environment_profile() -> EnvironmentProfile:
    """Process-wide profile, loaded once. Callers must let errors propagate."""
    return load_environment_profile()


def reset_environment_profile_cache() -> None:
    """Testing hook: forget the cached profile."""
    get_environment_profile.cache_clear()
