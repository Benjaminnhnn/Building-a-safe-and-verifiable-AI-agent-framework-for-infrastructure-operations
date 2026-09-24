"""Ablation configurations for RQ1 (no-safety-gate) and RQ2 (no-verifier)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Default ablation scenario set (representative subset of Moodle scenarios)
# ---------------------------------------------------------------------------

DEFAULT_ABLATION_SCENARIOS: list[str] = ["DB-01", "RES-01", "NET-01", "CON-01", "SEC-02"]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AblationMode(str, Enum):
    """Which component of the safety architecture is removed."""

    NONE = "none"
    NO_SAFETY_GATE = "no_safety_gate"
    NO_VERIFIER = "no_verifier"


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class AblationConfig(BaseModel):
    """Configuration for a single ablation experiment."""

    model_config = ConfigDict(extra="forbid")

    mode: AblationMode
    description: str
    shadow_only: bool = True
    """Ablation runs must not perform real dangerous mutations; dangerous
    actions are intercepted by the sandbox and recorded as 'would_execute'."""
    counterfactual_only: bool = True
    """For NO_VERIFIER: record what health-only verdict would be, but do not
    change the canonical RESOLVED authority."""
    scenarios: list[str]
    min_repetitions: int = 5


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def NoSafetyGateConfig() -> AblationConfig:  # noqa: N802 – matches task spec name
    """Return the ablation configuration for RQ1 (no safety gate).

    Dangerous actions that would normally be blocked by the Safety Policy
    Engine are intercepted by the sandbox; their identifiers are recorded
    in ``shadow_would_execute`` on the BenchmarkRun.  No real mutation
    occurs on the staging environment.
    """
    return AblationConfig(
        mode=AblationMode.NO_SAFETY_GATE,
        shadow_only=True,
        counterfactual_only=True,
        description=(
            'Record "would_execute" actions without gate; '
            "dangerous actions intercepted by sandbox"
        ),
        scenarios=DEFAULT_ABLATION_SCENARIOS,
        min_repetitions=5,
    )


def NoVerifierConfig() -> AblationConfig:  # noqa: N802 – matches task spec name
    """Return the ablation configuration for RQ2 (no verifier).

    The Independent Verifier is bypassed; a health-only probe is used to
    determine recovery.  The result is recorded as a counterfactual verdict
    on the BenchmarkRun.  The canonical RESOLVED authority is unchanged —
    the verifier still transitions state in the real pipeline.
    """
    return AblationConfig(
        mode=AblationMode.NO_VERIFIER,
        shadow_only=True,
        counterfactual_only=True,
        description=(
            "Record counterfactual health-only verdict; "
            "canonical RESOLVED authority unchanged"
        ),
        scenarios=DEFAULT_ABLATION_SCENARIOS,
        min_repetitions=5,
    )
