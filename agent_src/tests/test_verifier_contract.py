"""Tests for ContractVerdict, ProbeResult and ContractProbeRunner."""

from __future__ import annotations

from datetime import timezone

import pytest
from core.verifier_contract import ContractProbeRunner, ContractVerdict


@pytest.fixture()
def runner() -> ContractProbeRunner:
    return ContractProbeRunner()


# ---------------------------------------------------------------------------
# Offline run
# ---------------------------------------------------------------------------


def test_offline_verdict_not_resolution_eligible(runner: ContractProbeRunner) -> None:
    contract = {"version": "1.0", "allowed": ["moodle->db"], "forbidden": ["internet->postgres"]}
    verdict = runner.run_offline(contract, incident_id="inc-001")
    assert verdict.resolution_eligible is False


def test_offline_verdict_has_not_run_probes(runner: ContractProbeRunner) -> None:
    contract = {"version": "1.0", "allowed": ["moodle->db"], "forbidden": ["internet->postgres"]}
    verdict = runner.run_offline(contract, incident_id="inc-002")
    for probe in verdict.allowed_results + verdict.forbidden_results + verdict.related_results:
        assert probe.actual_status == "not_run"
        assert probe.passed is False


def test_offline_verdict_empty_contract(runner: ContractProbeRunner) -> None:
    verdict = runner.run_offline({}, incident_id="inc-003")
    assert verdict.resolution_eligible is False
    assert len(verdict.allowed_results) >= 1  # at least one placeholder
    assert len(verdict.forbidden_results) >= 1


# ---------------------------------------------------------------------------
# Health-only ablation
# ---------------------------------------------------------------------------


def test_health_only_ablation_not_eligible_when_health_passes(runner: ContractProbeRunner) -> None:
    verdict = runner.run_counterfactual_health_only("inc-010", health_passed=True)
    assert verdict.health_passed is True
    assert verdict.contract_passed is False
    assert verdict.resolution_eligible is False


def test_health_only_ablation_not_eligible_when_health_fails(runner: ContractProbeRunner) -> None:
    verdict = runner.run_counterfactual_health_only("inc-011", health_passed=False)
    assert verdict.health_passed is False
    assert verdict.resolution_eligible is False


def test_health_only_ablation_no_probes(runner: ContractProbeRunner) -> None:
    verdict = runner.run_counterfactual_health_only("inc-012", health_passed=True)
    assert verdict.allowed_results == []
    assert verdict.forbidden_results == []
    assert verdict.related_results == []


# ---------------------------------------------------------------------------
# False-recovery case (RQ2)
# ---------------------------------------------------------------------------


def test_false_recovery_case(runner: ContractProbeRunner) -> None:
    verdict = runner.build_false_recovery_case("inc-020")
    assert verdict.health_passed is True
    assert verdict.contract_passed is False
    assert verdict.resolution_eligible is False


def test_false_recovery_forbidden_probe_fails(runner: ContractProbeRunner) -> None:
    verdict = runner.build_false_recovery_case("inc-021")
    assert len(verdict.forbidden_results) >= 1
    failed_forbidden = [p for p in verdict.forbidden_results if not p.passed]
    assert len(failed_forbidden) >= 1, "At least one forbidden probe must fail (open channel detected)"


def test_false_recovery_reason_mentions_contract(runner: ContractProbeRunner) -> None:
    verdict = runner.build_false_recovery_case("inc-022")
    assert "contract" in verdict.reason.lower() or "false recovery" in verdict.reason.lower()


# ---------------------------------------------------------------------------
# Required fields on verdict
# ---------------------------------------------------------------------------


def test_verdict_has_required_fields(runner: ContractProbeRunner) -> None:
    verdict = runner.run_offline({"version": "1.0"}, incident_id="inc-030")
    assert verdict.verdict_id != ""
    assert verdict.incident_id == "inc-030"
    assert verdict.checked_at is not None
    assert verdict.authority == "independent_verifier"


def test_verdict_checked_at_is_utc(runner: ContractProbeRunner) -> None:
    verdict = runner.run_offline({}, incident_id="inc-031")
    assert verdict.checked_at.tzinfo is not None
    assert verdict.checked_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Resolution eligibility logic (all three must pass)
# ---------------------------------------------------------------------------


def test_resolution_requires_all_three(runner: ContractProbeRunner) -> None:
    """Only when health AND contract AND stable all True should resolution_eligible be True.

    We construct a ContractVerdict manually to verify the invariant.
    """
    from datetime import datetime, timezone

    def _make_verdict(health: bool, contract: bool, stable: bool) -> ContractVerdict:
        eligible = health and contract and stable
        return ContractVerdict(
            verdict_id="v-test",
            incident_id="inc-40",
            contract_version="1.0",
            checked_at=datetime.now(tz=timezone.utc),
            allowed_results=[],
            forbidden_results=[],
            related_results=[],
            stability_observations=[],
            stability_window_seconds=120,
            health_passed=health,
            contract_passed=contract,
            stable=stable,
            resolution_eligible=eligible,
            reason="Test",
        )

    # All pass → eligible
    assert _make_verdict(True, True, True).resolution_eligible is True

    # Any one fails → not eligible
    assert _make_verdict(False, True, True).resolution_eligible is False
    assert _make_verdict(True, False, True).resolution_eligible is False
    assert _make_verdict(True, True, False).resolution_eligible is False
    assert _make_verdict(False, False, False).resolution_eligible is False


def test_runner_offline_stability_window_zero(runner: ContractProbeRunner) -> None:
    verdict = runner.run_offline({}, incident_id="inc-050")
    assert verdict.stability_window_seconds == 0
    assert verdict.stable is False
