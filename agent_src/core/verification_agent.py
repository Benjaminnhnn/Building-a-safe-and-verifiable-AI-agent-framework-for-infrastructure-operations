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
from typing import Any, Protocol, runtime_checkable

from core.schema.incident import Incident
from core.schema.verification import ProbeResult, VerificationResult


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
        self._details = details or f"dry-run {probe_type} probe {'passed' if passes else 'FAILED'}"
    
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
    ) -> VerificationResult:
        """Run verification probes and determine verdict."""
        
        # Build probes from contract if none provided
        if probes is None:
            probes = self._build_dry_run_probes(communication_contract)
        
        # Run all probes
        allowed_results: list[ProbeResult] = []
        forbidden_results: list[ProbeResult] = []
        related_results: list[ProbeResult] = []
        
        for probe in probes:
            result = probe.check()
            if probe.probe_type == "allowed":
                allowed_results.append(result)
            elif probe.probe_type == "forbidden":
                forbidden_results.append(result)
            elif probe.probe_type == "related":
                related_results.append(result)
        
        # Determine health
        health_passed = all(r.passed for r in allowed_results) if allowed_results else True
        
        # Determine contract compliance
        # For forbidden probes: "passed" means the forbidden path is STILL BLOCKED (good)
        forbidden_ok = all(r.passed for r in forbidden_results) if forbidden_results else True
        related_ok = all(r.passed for r in related_results) if related_results else True
        contract_passed = forbidden_ok and related_ok
        
        # Determine verdict
        verdict = self._determine_verdict(
            health_passed=health_passed,
            contract_passed=contract_passed,
            forbidden_ok=forbidden_ok,
            related_ok=related_ok,
            stability_seconds=stability_window_seconds,
        )
        
        return VerificationResult(
            verification_id=_stable_id("verify", incident.incident_id),
            incident_id=incident.incident_id,
            health_passed=health_passed,
            communication_contract_passed=contract_passed,
            stability_seconds=stability_window_seconds,
            allowed_probes=allowed_results,
            forbidden_probes=forbidden_results,
            related_probes=related_results,
            verdict=verdict,
            simulated=dry_run,
        )
    
    def _determine_verdict(
        self,
        *,
        health_passed: bool,
        contract_passed: bool,
        forbidden_ok: bool,
        related_ok: bool,
        stability_seconds: int,
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
        
        if stability_seconds < 120:
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
            probes.append(DryRunProbe(
                name=name, probe_type="forbidden", passes=True,
                details=f"dry-run: {name} is still blocked (good)",
            ))
        for name in contract.get("related", []):
            probes.append(DryRunProbe(name=name, probe_type="related", passes=True))
        return probes
