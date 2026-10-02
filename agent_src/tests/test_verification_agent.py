"""Tests for the Verification Agent."""

from core.schema.incident import Incident
from core.schema.verification import StabilityObservation
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

    def test_all_probes_pass_resolved(self):
        result = self.agent.verify(self.incident, self.contract)
        assert result.verdict == "resolved"
        assert result.health_passed
        assert result.communication_contract_passed

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

    def test_live_verification_resolves_after_continuous_healthy_window(self):
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

    def test_verifier_has_no_action_execution_api(self):
        assert not hasattr(self.agent, "execute")
