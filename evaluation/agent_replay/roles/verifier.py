"""Verifier module — wraps Phase 3 IndependentVerifier into pipeline envelope.

Invariants:
1. STRICT ISOLATION: Verifier has ZERO imports from any executor.
2. Read-only probe authority: verifies health, communication contract, and scenario probes.
3. Requires N consecutive stable rounds for RESTORED verdict.
4. Preserves incident_id and evidence_ids.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.roles.contracts import BaseRoleMessage
from evaluation.agent_replay.verification.communication_contract import (
    CommunicationContract,
    load_communication_contract,
)
from evaluation.agent_replay.verification.independent_verifier import (
    IndependentVerifier,
    RQ2DivergenceTracker,
    VerificationReport,
    VerifierVerdict,
)


class VerifierInput(BaseRoleMessage):
    """Input payload for the Verifier role."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    scenario_id: str = Field(..., description="Scenario identifier being evaluated.")
    plan_hash: str = Field(..., description="Canonical hash of the plan to verify.")
    contract: CommunicationContract | None = Field(
        default=None,
        description="Communication contract model.",
    )
    contract_path: str | None = Field(
        default=None,
        description="Path to communication contract YAML file if contract object is not provided.",
    )
    n_consecutive_checks: int = Field(
        default=3,
        ge=1,
        description="Number of consecutive stable rounds required for RESTORED.",
    )
    health_probe_fn: Any | None = Field(default=None)
    allowed_probe_fn: Any | None = Field(default=None)
    forbidden_probe_fn: Any | None = Field(default=None)
    scenario_probe_fn: Any | None = Field(default=None)
    divergence_tracker: RQ2DivergenceTracker | None = Field(
        default=None,
        description="Tracker for recording RQ2 divergence metrics.",
    )


class VerifierOutput(BaseRoleMessage):
    """Output envelope from the Verifier role."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plan_hash: str = Field(..., description="Plan hash verified against.")
    verification_report: VerificationReport = Field(
        ...,
        description="Full report including round observations and check details.",
    )
    verdict: VerifierVerdict = Field(
        ...,
        description="RESTORED, NOT_RESTORED, or INCONCLUSIVE.",
    )


class VerifierRole:
    """Independent Verifier component with read-only probe authority."""

    def verify(self, input_data: VerifierInput) -> VerifierOutput:
        contract = input_data.contract
        if contract is None and input_data.contract_path:
            contract = load_communication_contract(input_data.contract_path)
        if contract is None:
            default_contract_path = (
                Path(__file__).resolve().parents[2]
                / "config"
                / "contracts"
                / "moodle_communication_contract.yaml"
            )
            if default_contract_path.is_file():
                contract = load_communication_contract(default_contract_path)
        if contract is None:
            raise ValueError("Either contract or contract_path must be supplied to VerifierRole")


        verifier = IndependentVerifier(
            contract=contract,
            n_consecutive_checks=input_data.n_consecutive_checks,
            divergence_tracker=input_data.divergence_tracker,
        )

        report = verifier.verify(
            incident_id=input_data.incident_id,
            scenario_id=input_data.scenario_id,
            plan_hash=input_data.plan_hash,
            health_probe_fn=input_data.health_probe_fn,
            allowed_probe_fn=input_data.allowed_probe_fn,
            forbidden_probe_fn=input_data.forbidden_probe_fn,
            scenario_probe_fn=input_data.scenario_probe_fn,
        )

        return VerifierOutput(
            incident_id=input_data.incident_id,
            evidence_ids=input_data.evidence_ids,
            plan_hash=input_data.plan_hash,
            verification_report=report,
            verdict=report.verdict,
        )
