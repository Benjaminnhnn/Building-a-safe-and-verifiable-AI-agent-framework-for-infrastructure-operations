"""Independent Verification Agent — answers RQ2.

The Verification Agent:
- Has NO execution credentials (separation of privilege)
- Is the ONLY agent that can transition an incident to RESOLVED
- Checks 3 types of probes: allowed, forbidden, related
- Detects false recovery (health OK but contract violated)
- Enforces stability window
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from core.schema.incident import Incident
from core.schema.verification import (
    ProbeResult,
    StabilityObservation,
    VerificationResult,
)


@runtime_checkable
class ContractProbe(Protocol):
    """Protocol for verification probes."""

    name: str
    probe_type: str  # "allowed", "forbidden", "related"

    def check(self) -> ProbeResult: ...


class DryRunProbe:
    """Dry-run probe that returns configurable results."""

    def __init__(
        self,
        name: str,
        probe_type: str,
        *,
        passes: bool = True,
        details: str = "",
    ) -> None:
        self.name = name
        self.probe_type = probe_type
        self._passes = passes
        self._details = (
            details or f"dry-run {probe_type} probe {'passed' if passes else 'FAILED'}"
        )

    def check(self) -> ProbeResult:
        return ProbeResult(
            name=self.name,
            passed=self._passes,
            details=self._details,
        )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class VerificationAgent:
    """Independent Verifier for communication contract compliance.

    Key design rules:
    1. VerificationAgent has NO execution credentials
    2. ONLY VerificationAgent can transition incident to RESOLVED
    3. Health check alone is NOT sufficient — contract must also pass
    4. Stability window must be satisfied
    """

    def verify(
        self,
        incident: Incident,
        communication_contract: dict[str, Any],
        *,
        stability_window_seconds: int = 120,
        dry_run: bool = True,
        probes: list[ContractProbe] | None = None,
        stability_observations: list[StabilityObservation] | None = None,
        max_stability_gap_seconds: int = 60,
    ) -> VerificationResult:
        """Run verification probes and determine verdict."""
        if (
            isinstance(stability_window_seconds, bool)
            or not isinstance(stability_window_seconds, int)
            or stability_window_seconds < 120
        ):
            raise ValueError("stability_window_seconds must be an integer of at least 120")
        if (
            isinstance(max_stability_gap_seconds, bool)
            or not isinstance(max_stability_gap_seconds, int)
            or not 1 <= max_stability_gap_seconds <= 60
        ):
            raise ValueError("max_stability_gap_seconds must be an integer from 1 to 60")

        contract_specs: dict[str, list[str]] = {}
        contract_valid = isinstance(communication_contract, dict)
        for probe_type in ("allowed", "forbidden", "related"):
            raw_names = (
                communication_contract.get(probe_type)
                if contract_valid
                else None
            )
            if not isinstance(raw_names, list):
                contract_specs[probe_type] = []
                contract_valid = False
                continue
            names = [
                name for name in raw_names
                if isinstance(name, str) and name.strip()
            ]
            if len(names) != len(raw_names) or len(set(names)) != len(names):
                contract_valid = False
            contract_specs[probe_type] = names

        # Synthetic probes are allowed only for explicitly marked fixture replay.
        if probes is None:
            probes = self._build_dry_run_probes(contract_specs) if dry_run else []

        if stability_observations is None and dry_run:
            checked_at = datetime.now(timezone.utc)
            intervals = max(1, math.ceil(stability_window_seconds / max_stability_gap_seconds))
            stability_observations = [
                StabilityObservation(
                    observed_at=checked_at
                    - timedelta(
                        seconds=stability_window_seconds
                        - (stability_window_seconds * index / intervals)
                    ),
                    healthy=True,
                )
                for index in range(intervals + 1)
            ]
        stability_valid = stability_observations is None or isinstance(
            stability_observations, list
        )
        normalized_stability: list[StabilityObservation] = []
        for observation in stability_observations or []:
            try:
                payload = (
                    observation.__dict__
                    if isinstance(observation, StabilityObservation)
                    else observation
                )
                normalized_stability.append(StabilityObservation.model_validate(payload))
            except Exception:
                stability_valid = False
        stability_observations = normalized_stability
        measured_stability_seconds = _stability_span(stability_observations)
        stability_gaps_ok = _stability_gaps_ok(
            stability_observations, max_stability_gap_seconds
        )
        stability_gaps_ok = stability_valid and stability_gaps_ok and _stability_observations_fresh(
            stability_observations, max_staleness_seconds=max_stability_gap_seconds
        )

        # Run all probes
        allowed_results: list[ProbeResult] = []
        forbidden_results: list[ProbeResult] = []
        related_results: list[ProbeResult] = []
        simulated = dry_run or any(item.simulated for item in stability_observations)
        probes_valid = True

        for probe in probes:
            name = getattr(probe, "name", None)
            probe_type = getattr(probe, "probe_type", None)
            check = getattr(probe, "check", None)
            if (
                not isinstance(name, str)
                or not name.strip()
                or probe_type not in ("allowed", "forbidden", "related")
                or not callable(check)
            ):
                probes_valid = False
                continue
            try:
                result = check()
            except Exception as exc:
                result = ProbeResult(
                    name=name,
                    passed=False,
                    details=f"probe execution failed ({type(exc).__name__})",
                )
            # A probe's result is evidence only for the probe that was asked to
            # run. Do not let a misconfigured adapter relabel another check.
            if isinstance(result, ProbeResult):
                try:
                    result = ProbeResult.model_validate(result.__dict__)
                except Exception:
                    probes_valid = False
                    result = None
            if not isinstance(result, ProbeResult) or result.name != name:
                probes_valid = False
                result = ProbeResult(
                    name=name,
                    passed=False,
                    details="probe result identity did not match the requested probe",
                )
            simulated = simulated or result.simulated
            if probe_type == "allowed":
                allowed_results.append(result)
            elif probe_type == "forbidden":
                forbidden_results.append(result)
            else:
                related_results.append(result)

        # Determine health
        expected_by_type = {
            probe_type: {(probe_type, name) for name in contract_specs[probe_type]}
            for probe_type in ("allowed", "forbidden", "related")
        }
        expected = set().union(*expected_by_type.values())
        observed = [
            (probe.probe_type, probe.name)
            for probe in probes
            if getattr(probe, "probe_type", None)
            in ("allowed", "forbidden", "related")
            and isinstance(getattr(probe, "name", None), str)
        ]
        coverage_ok = (
            contract_valid
            and probes_valid
            and
            all(expected_by_type.values())
            and len(observed) == len(set(observed))
            and set(observed) == expected
        )

        health_passed = bool(allowed_results) and all(r.passed for r in allowed_results)
        if stability_observations:
            health_passed = health_passed and all(
                observation.healthy for observation in stability_observations
            )

        # Determine contract compliance
        # For forbidden probes: "passed" means the forbidden path is STILL BLOCKED (good)
        forbidden_ok = (
            all(r.passed for r in forbidden_results) if forbidden_results else True
        )
        related_ok = all(r.passed for r in related_results) if related_results else True
        contract_passed = forbidden_ok and related_ok

        # Determine verdict
        contract_passed = contract_passed and coverage_ok
        verdict = self._determine_verdict(
            health_passed=health_passed,
            contract_passed=contract_passed,
            forbidden_ok=forbidden_ok,
            related_ok=related_ok,
            stability_seconds=measured_stability_seconds,
            required_stability_seconds=stability_window_seconds,
            stability_gaps_ok=stability_gaps_ok,
        )

        return VerificationResult(
            verification_id=_stable_id("verify", incident.incident_id),
            incident_id=incident.incident_id,
            health_passed=health_passed,
            communication_contract_passed=contract_passed,
            stability_seconds=measured_stability_seconds,
            stability_observations=stability_observations,
            resolution_eligible=verdict == "resolved",
            allowed_probes=allowed_results,
            forbidden_probes=forbidden_results,
            related_probes=related_results,
            verdict=verdict,
            simulated=simulated,
        )

    def _determine_verdict(
        self,
        *,
        health_passed: bool,
        contract_passed: bool,
        forbidden_ok: bool,
        related_ok: bool,
        stability_seconds: int,
        required_stability_seconds: int,
        stability_gaps_ok: bool,
    ) -> str:
        """Determine verification verdict.

        Key RQ2 insight: health_passed alone is NOT sufficient.
        Contract must also be verified.
        """
        if not health_passed:
            return "not_resolved"

        if health_passed and not forbidden_ok:
            # Health is OK but forbidden paths are accessible
            # This is FALSE RECOVERY — the main RQ2 finding
            return "false_recovery"

        if health_passed and not related_ok:
            # Health is OK but related services are damaged
            # Collateral damage from the remediation
            return "collateral_damage"

        if stability_seconds < required_stability_seconds or not stability_gaps_ok:
            return "not_stable"

        if health_passed and contract_passed:
            return "resolved"

        return "not_resolved"

    def _build_dry_run_probes(
        self,
        contract: dict[str, Any],
    ) -> list[ContractProbe]:
        """Build dry-run probes from communication contract dict."""
        probes: list[ContractProbe] = []
        for name in contract.get("allowed", []):
            probes.append(DryRunProbe(name=name, probe_type="allowed", passes=True))
        for name in contract.get("forbidden", []):
            probes.append(
                DryRunProbe(
                    name=name,
                    probe_type="forbidden",
                    passes=True,
                    details=f"dry-run: {name} is still blocked (good)",
                )
            )
        for name in contract.get("related", []):
            probes.append(DryRunProbe(name=name, probe_type="related", passes=True))
        return probes


def _stability_span(observations: list[StabilityObservation]) -> int:
    """Return observed healthy window; caller-supplied duration is never trusted."""
    if len(observations) < 2:
        return 0
    timestamps = [item.observed_at for item in observations]
    if timestamps != sorted(timestamps):
        return 0
    return max(0, int((timestamps[-1] - timestamps[0]).total_seconds()))


def _stability_gaps_ok(
    observations: list[StabilityObservation], max_gap_seconds: int
) -> bool:
    if max_gap_seconds < 1 or len(observations) < 2:
        return False
    timestamps = [item.observed_at for item in observations]
    if timestamps != sorted(timestamps):
        return False
    return all(
        0 < (later - earlier).total_seconds() <= max_gap_seconds
        for earlier, later in zip(timestamps, timestamps[1:])
    )


def _stability_observations_fresh(
    observations: list[StabilityObservation], *, max_staleness_seconds: int
) -> bool:
    if not observations:
        return False
    latest = max(item.observed_at for item in observations).astimezone(timezone.utc)
    age = (datetime.now(timezone.utc) - latest).total_seconds()
    return -30 <= age <= max_staleness_seconds
