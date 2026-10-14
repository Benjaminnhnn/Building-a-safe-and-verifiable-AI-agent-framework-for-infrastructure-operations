"""Tests for the Verification Agent."""

import pytest

from core.schema.incident import Incident
from core.schema.verification import ProbeResult, StabilityObservation
from core.verification_agent import DryRunProbe, VerificationAgent
from datetime import datetime, timedelta, timezone


def _make_incident() -> Incident:
    return Incident(
        incident_id="inc-test-001",
        fingerprint="fp-test",
        title="Test incident",
        severity="critical",
        affected_resources=["postgres-db"],
    )


def _sample_contract() -> dict:
    return {
        "allowed": ["client_to_moodle", "moodle_to_database"],
        "forbidden": ["internet_to_postgresql"],
        "related": ["prometheus_scrape"],
    }


class TestVerificationAgent:
    def setup_method(self):
        self.agent = VerificationAgent()
        self.incident = _make_incident()
        self.contract = _sample_contract()

    def test_stability_configuration_enforces_thesis_window_and_sampling_gap(self):
        with pytest.raises(ValueError, match="at least 120"):
            self.agent.verify(self.incident, self.contract, stability_window_seconds=119)
        with pytest.raises(ValueError, match="from 1 to 60"):
            self.agent.verify(self.incident, self.contract, max_stability_gap_seconds=61)
        with pytest.raises(ValueError, match="integer of at least 120"):
            self.agent.verify(self.incident, self.contract, stability_window_seconds=True)

    def test_dry_run_probes_are_always_marked_simulated(self):
        result = self.agent.verify(self.incident, self.contract)
        assert result.verdict == "resolved"
        assert result.health_passed
        assert result.communication_contract_passed
        assert result.simulated
        assert result.resolution_eligible

    def test_health_fail_not_resolved(self):
        probes = [
            DryRunProbe(
                "client_to_moodle",
                "allowed",
                passes=False,
                details="connection refused",
            ),
            DryRunProbe("internet_to_postgresql", "forbidden", passes=True),
            DryRunProbe("prometheus_scrape", "related", passes=True),
        ]
        result = self.agent.verify(self.incident, self.contract, probes=probes)
        assert result.verdict == "not_resolved"
        assert not result.health_passed

    def test_false_recovery_health_ok_forbidden_fail(self):
        """RQ2 key test: health passes but forbidden path is accessible."""
        probes = [
            DryRunProbe("client_to_moodle", "allowed", passes=True),
            DryRunProbe("moodle_to_database", "allowed", passes=True),
            DryRunProbe(
                "internet_to_postgresql",
                "forbidden",
                passes=False,
                details="VULNERABLE: port 5432 accessible from internet",
            ),
            DryRunProbe("prometheus_scrape", "related", passes=True),
        ]
        result = self.agent.verify(self.incident, self.contract, probes=probes)
        assert result.verdict == "false_recovery"
        assert result.health_passed
        assert not result.communication_contract_passed

    def test_collateral_damage_related_fail(self):
        probes = [
            DryRunProbe("client_to_moodle", "allowed", passes=True),
            DryRunProbe("internet_to_postgresql", "forbidden", passes=True),
            DryRunProbe(
                "prometheus_scrape",
                "related",
                passes=False,
                details="Prometheus scrape target down after remediation",
            ),
        ]
        result = self.agent.verify(self.incident, self.contract, probes=probes)
        assert result.verdict == "collateral_damage"
        assert result.health_passed
        assert not result.communication_contract_passed

    def test_simulated_flag(self):
        result = self.agent.verify(self.incident, self.contract, dry_run=True)
        assert result.simulated

    def test_live_verifier_requires_observed_stability_window(self):
        probes = [
            DryRunProbe("client_to_moodle", "allowed"),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]
        result = self.agent.verify(
            self.incident, self.contract, dry_run=False, probes=probes
        )
        assert result.verdict == "not_stable"
        assert result.stability_seconds == 0
        assert not result.resolution_eligible

    def test_live_window_is_measured_from_read_only_observations(self):
        checked_at = datetime.now(timezone.utc)
        observations = [
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=119), healthy=True
            ),
            StabilityObservation(observed_at=checked_at, healthy=True),
        ]
        probes = [
            DryRunProbe("client_to_moodle", "allowed"),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]
        result = self.agent.verify(
            self.incident,
            self.contract,
            stability_window_seconds=120,
            dry_run=False,
            probes=probes,
            stability_observations=observations,
        )
        assert result.stability_seconds == 119
        assert result.verdict == "not_stable"

    def test_fixture_probe_results_cannot_be_mislabeled_live(self):
        checked_at = datetime.now(timezone.utc)
        observations = [
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=120), healthy=True
            ),
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=60), healthy=True
            ),
            StabilityObservation(observed_at=checked_at, healthy=True),
        ]
        probes = [
            DryRunProbe("client_to_moodle", "allowed"),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]
        result = self.agent.verify(
            self.incident,
            self.contract,
            dry_run=False,
            probes=probes,
            stability_observations=observations,
        )
        assert result.verdict == "resolved"
        assert result.resolution_eligible
        assert result.simulated
        assert result.stability_seconds == 120

    def test_large_gap_does_not_count_as_continuous_stability(self):
        checked_at = datetime.now(timezone.utc)
        observations = [
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=120), healthy=True
            ),
            StabilityObservation(observed_at=checked_at, healthy=True),
        ]
        probes = [
            DryRunProbe("client_to_moodle", "allowed"),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]
        result = self.agent.verify(
            self.incident,
            self.contract,
            dry_run=False,
            probes=probes,
            stability_observations=observations,
        )
        assert result.stability_seconds == 120
        assert result.verdict == "not_stable"

    def test_stale_stability_window_cannot_resolve_current_incident(self):
        checked_at = datetime.now(timezone.utc) - timedelta(seconds=180)
        observations = [
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=120), healthy=True
            ),
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=60), healthy=True
            ),
            StabilityObservation(observed_at=checked_at, healthy=True),
        ]
        probes = [
            DryRunProbe("client_to_moodle", "allowed"),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]

        result = self.agent.verify(
            self.incident,
            self.contract,
            dry_run=False,
            probes=probes,
            stability_observations=observations,
        )

        assert result.verdict == "not_stable"
        assert not result.resolution_eligible

    def test_out_of_order_stability_samples_cannot_resolve(self):
        checked_at = datetime.now(timezone.utc)
        observations = [
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=60), healthy=True
            ),
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=120), healthy=True
            ),
            StabilityObservation(observed_at=checked_at, healthy=True),
        ]
        result = self.agent.verify(
            self.incident,
            self.contract,
            dry_run=False,
            probes=[
                DryRunProbe("client_to_moodle", "allowed"),
                DryRunProbe("moodle_to_database", "allowed"),
                DryRunProbe("internet_to_postgresql", "forbidden"),
                DryRunProbe("prometheus_scrape", "related"),
            ],
            stability_observations=observations,
        )

        assert result.verdict == "not_stable"
        assert result.stability_seconds == 0

    def test_health_only_trap_is_detected_for_all_repeated_fixtures(self):
        detected = 0
        for _ in range(10):
            probes = [
                DryRunProbe("client_to_moodle", "allowed", passes=True),
                DryRunProbe("moodle_to_database", "allowed", passes=True),
                DryRunProbe("internet_to_postgresql", "forbidden", passes=False),
                DryRunProbe("prometheus_scrape", "related", passes=True),
            ]
            result = self.agent.verify(self.incident, self.contract, probes=probes)
            detected += result.verdict == "false_recovery"
        assert detected / 10 >= 0.90

    def test_missing_contract_probe_fails_closed(self):
        probes = [
            DryRunProbe("client_to_moodle", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]
        result = self.agent.verify(self.incident, self.contract, probes=probes)
        assert result.verdict == "not_resolved"
        assert not result.resolution_eligible

    def test_malformed_contract_and_probe_adapter_fail_closed(self):
        malformed_contract = {
            "allowed": "client_to_moodle",
            "forbidden": ["internet_to_postgresql"],
            "related": ["prometheus_scrape"],
        }
        contract_result = self.agent.verify(self.incident, malformed_contract)
        assert contract_result.verdict == "not_resolved"
        assert not contract_result.communication_contract_passed
        assert not contract_result.resolution_eligible

        class MalformedProbe:
            probe_type = "allowed"

            def check(self):
                return ProbeResult(name="client_to_moodle", passed=True, details="ok")

        probes = [
            MalformedProbe(),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]
        probe_result = self.agent.verify(self.incident, self.contract, probes=probes)
        assert probe_result.verdict == "not_resolved"
        assert not probe_result.communication_contract_passed
        assert not probe_result.resolution_eligible

    def test_contract_without_forbidden_or_related_probes_cannot_resolve(self):
        incomplete_contract = {
            "allowed": ["client_to_moodle"],
            "forbidden": [],
            "related": [],
        }
        result = self.agent.verify(self.incident, incomplete_contract)
        assert result.health_passed
        assert not result.communication_contract_passed
        assert result.verdict == "not_resolved"
        assert not result.resolution_eligible

    def test_probe_result_cannot_be_relabelled(self):
        class MislabelledProbe(DryRunProbe):
            def check(self):
                return ProbeResult(
                    name="unrelated_probe", passed=True, details="wrong identity"
                )

        probes = [
            MislabelledProbe("client_to_moodle", "allowed"),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]

        result = self.agent.verify(self.incident, self.contract, probes=probes)

        assert result.verdict == "not_resolved"
        assert not result.health_passed
        assert result.allowed_probes[0].name == "client_to_moodle"
        assert not result.allowed_probes[0].passed
        assert "identity" in result.allowed_probes[0].details

    def test_probe_model_copy_with_invalid_boolean_is_rejected(self):
        class InvalidCopyProbe(DryRunProbe):
            def check(self):
                return ProbeResult(
                    name=self.name, passed=True, details="ok"
                ).model_copy(update={"passed": 1})

        result = self.agent.verify(
            self.incident,
            self.contract,
            probes=[
                InvalidCopyProbe("client_to_moodle", "allowed"),
                DryRunProbe("moodle_to_database", "allowed"),
                DryRunProbe("internet_to_postgresql", "forbidden"),
                DryRunProbe("prometheus_scrape", "related"),
            ],
        )

        assert result.verdict == "not_resolved"
        assert not result.health_passed
        assert not result.allowed_probes[0].passed

    def test_stability_model_copy_with_invalid_boolean_is_rejected(self):
        checked_at = datetime.now(timezone.utc)
        observations = [
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=120), healthy=True
            ),
            StabilityObservation(
                observed_at=checked_at - timedelta(seconds=60), healthy=True
            ).model_copy(update={"healthy": 1}),
            StabilityObservation(observed_at=checked_at, healthy=True),
        ]
        result = self.agent.verify(
            self.incident,
            self.contract,
            dry_run=False,
            probes=[
                DryRunProbe("client_to_moodle", "allowed"),
                DryRunProbe("moodle_to_database", "allowed"),
                DryRunProbe("internet_to_postgresql", "forbidden"),
                DryRunProbe("prometheus_scrape", "related"),
            ],
            stability_observations=observations,
        )

        assert result.verdict == "not_stable"
        assert not result.resolution_eligible
        assert len(result.stability_observations) == 2

    def test_probe_exception_is_recorded_as_failed_evidence(self):
        class RaisingProbe(DryRunProbe):
            def check(self):
                raise TimeoutError("sensitive transport details")

        probes = [
            RaisingProbe("client_to_moodle", "allowed"),
            DryRunProbe("moodle_to_database", "allowed"),
            DryRunProbe("internet_to_postgresql", "forbidden"),
            DryRunProbe("prometheus_scrape", "related"),
        ]

        result = self.agent.verify(self.incident, self.contract, probes=probes)

        assert result.verdict == "not_resolved"
        assert not result.health_passed
        assert result.allowed_probes[0].name == "client_to_moodle"
        assert not result.allowed_probes[0].passed
        assert "TimeoutError" in result.allowed_probes[0].details
        assert "sensitive transport details" not in result.allowed_probes[0].details

    def test_verifier_has_no_action_execution_api(self):
        assert not hasattr(self.agent, "execute")
