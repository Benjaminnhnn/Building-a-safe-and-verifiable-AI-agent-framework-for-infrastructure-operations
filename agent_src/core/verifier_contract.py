"""Independent Verifier contract schema and offline probe runner.

The verifier runs separately from executor with read-only credentials.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: str
    probe_type: Literal["allowed", "forbidden", "related"]
    expected_status: str  # "passed" for allowed/related, "blocked" for forbidden
    actual_status: str
    checked_at: datetime
    duration_ms: float
    passed: bool


class StabilityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str
    observed_at: datetime
    status: str
    health_checks: dict[str, bool]


class ContractVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict_id: str
    incident_id: str
    contract_version: str
    checked_at: datetime
    allowed_results: list[ProbeResult]
    forbidden_results: list[ProbeResult]
    related_results: list[ProbeResult]
    stability_observations: list[StabilityObservation]
    stability_window_seconds: int
    health_passed: bool
    contract_passed: bool
    stable: bool
    resolution_eligible: bool  # True only if health_passed AND contract_passed AND stable
    reason: str
    authority: str = "independent_verifier"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _not_run_probe(probe_type: Literal["allowed", "forbidden", "related"], index: int) -> ProbeResult:
    expected = "blocked" if probe_type == "forbidden" else "passed"
    return ProbeResult(
        probe_id=f"probe-{probe_type}-{index}",
        probe_type=probe_type,
        expected_status=expected,
        actual_status="not_run",
        checked_at=_utcnow(),
        duration_ms=0.0,
        passed=False,
    )


class ContractProbeRunner:
    """Runs contract probes against a deployed environment (or offline fixtures)."""

    # ------------------------------------------------------------------
    # Offline / replay
    # ------------------------------------------------------------------

    def run_offline(self, contract: dict, incident_id: str) -> ContractVerdict:
        """Return a verdict where all probes are 'not_run' and resolution_eligible=False.

        Used for replay/fixture scenarios where the live environment is unavailable.
        """
        allowed_specs = contract.get("allowed", [])
        forbidden_specs = contract.get("forbidden", [])
        related_specs = contract.get("related", [])

        allowed_results = [_not_run_probe("allowed", i) for i in range(max(len(allowed_specs), 1))]
        forbidden_results = [_not_run_probe("forbidden", i) for i in range(max(len(forbidden_specs), 1))]
        related_results = [_not_run_probe("related", i) for i in range(max(len(related_specs), 1))]

        return ContractVerdict(
            verdict_id=str(uuid.uuid4()),
            incident_id=incident_id,
            contract_version=contract.get("version", "1.0"),
            checked_at=_utcnow(),
            allowed_results=allowed_results,
            forbidden_results=forbidden_results,
            related_results=related_results,
            stability_observations=[],
            stability_window_seconds=0,
            health_passed=False,
            contract_passed=False,
            stable=False,
            resolution_eligible=False,
            reason="Offline run — probes not executed; resolution not eligible.",
        )

    # ------------------------------------------------------------------
    # Ablation: health-only (no verifier contract)
    # ------------------------------------------------------------------

    def run_counterfactual_health_only(
        self, incident_id: str, health_passed: bool
    ) -> ContractVerdict:
        """Simulate the health-only (no verifier) baseline for ablation.

        Even when health passes, resolution is not eligible because the
        contract was never verified.
        """
        reason = (
            "Health-only counterfactual: contract probes not run. "
            "Health passed but resolution requires independent contract verification."
            if health_passed
            else "Health-only counterfactual: health check failed. Resolution not eligible."
        )
        return ContractVerdict(
            verdict_id=str(uuid.uuid4()),
            incident_id=incident_id,
            contract_version="ablation-health-only",
            checked_at=_utcnow(),
            allowed_results=[],
            forbidden_results=[],
            related_results=[],
            stability_observations=[],
            stability_window_seconds=0,
            health_passed=health_passed,
            contract_passed=False,  # contract was never run
            stable=False,
            resolution_eligible=False,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Test fixture: false recovery case (RQ2)
    # ------------------------------------------------------------------

    def build_false_recovery_case(self, incident_id: str) -> ContractVerdict:
        """Create a test case where health=passed but contract=failed.

        Used to validate RQ2: the Independent Verifier must catch false
        recoveries that would be missed by health-check-only.
        """
        now = _utcnow()
        # Health checks pass — system appears alive
        stability_obs = StabilityObservation(
            observation_id=str(uuid.uuid4()),
            observed_at=now,
            status="healthy",
            health_checks={"http_200": True, "db_connection": True, "cpu_normal": True},
        )
        # But contract probes reveal forbidden channel is open
        forbidden_probe = ProbeResult(
            probe_id="probe-forbidden-internet-to-postgres",
            probe_type="forbidden",
            expected_status="blocked",
            actual_status="passed",  # channel is NOT blocked — contract violated
            checked_at=now,
            duration_ms=12.5,
            passed=False,  # expected blocked, got passed → fail
        )
        allowed_probe = ProbeResult(
            probe_id="probe-allowed-moodle-to-db",
            probe_type="allowed",
            expected_status="passed",
            actual_status="passed",
            checked_at=now,
            duration_ms=8.3,
            passed=True,
        )
        return ContractVerdict(
            verdict_id=str(uuid.uuid4()),
            incident_id=incident_id,
            contract_version="1.0",
            checked_at=now,
            allowed_results=[allowed_probe],
            forbidden_results=[forbidden_probe],
            related_results=[],
            stability_observations=[stability_obs],
            stability_window_seconds=120,
            health_passed=True,   # health passes
            contract_passed=False,  # contract fails
            stable=True,
            resolution_eligible=False,  # requires ALL three
            reason=(
                "False recovery detected: health checks pass but contract probe "
                "'probe-forbidden-internet-to-postgres' expected status='blocked', "
                "got status='passed'. Resolution NOT eligible."
            ),
        )
