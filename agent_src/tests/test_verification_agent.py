"""Tests for the Verification Agent."""

from core.schema.incident import Incident
from core.verification_agent import DryRunProbe, VerificationAgent


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
