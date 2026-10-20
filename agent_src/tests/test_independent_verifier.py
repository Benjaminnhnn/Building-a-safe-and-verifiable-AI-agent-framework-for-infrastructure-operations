"""Tests for Independent Verifier, Communication Contract, and RQ2 Divergence Tracking.

Mandatory verifications:
1. Divergence case: health passes but contract fails -> verdict is NOT_RESTORED,
   divergence metric recorded and persisted to file for RQ2.
2. Forbidden flow open: all allowed flows pass, health passes, but a forbidden flow
   is open -> verdict is NOT_RESTORED.
3. N consecutive stability checks: RESTORED is only achieved when all 4 check categories
   remain stable across N consecutive iterations.
4. Verifier does NOT import or call any executor module (enforced by AST import inspection test).
5. Hard rule: health pass + contract fail -> never RESTORED under any condition.
6. NOT_RESTORED or INCONCLUSIVE stops action chain, logs audit, notifies operator,
   and does not auto-rollback.
7. Correct loading and parsing of sample demo-web communication contract YAML.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.verification.communication_contract import (
    CommunicationContract,
    ConnectionRule,
    ServiceDefinition,
    load_communication_contract,
)
from evaluation.agent_replay.verification.independent_verifier import (
    IndependentVerifier,
    RQ2DivergenceTracker,
    VerifierVerdict,
)

SAMPLE_YAML_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "contracts"
    / "demo_web_communication_contract.yaml"
)


# ---------------------------------------------------------------------------
# Test Fixtures & Contract Builders
# ---------------------------------------------------------------------------


def _sample_demo_contract() -> CommunicationContract:
    """Build or load the sample demo-web communication contract."""
    if SAMPLE_YAML_PATH.is_file():
        return load_communication_contract(SAMPLE_YAML_PATH)

    return CommunicationContract(
        version="1.0",
        contract_name="demo-web-test-contract",
        sample_only=True,
        target_system="demo-web",
        description="Sample contract for demo-web",
        services={
            "frontend": ServiceDefinition(
                description="Frontend",
                health_endpoint="http://frontend:80/healthz",
                required_flows=["frontend_to_backend"],
            ),
            "backend": ServiceDefinition(
                description="Backend",
                health_endpoint="http://backend:8080/health",
                required_flows=["backend_to_database"],
            ),
            "database": ServiceDefinition(
                description="Database",
                health_endpoint="database:5432",
                required_flows=[],
            ),
        },
        allowed_connections=[
            ConnectionRule(
                id="frontend_to_backend",
                name="frontend_to_backend",
                source="frontend",
                destination="backend",
                port=8080,
                protocol="tcp",
            ),
            ConnectionRule(
                id="backend_to_database",
                name="backend_to_database",
                source="backend",
                destination="database",
                port=5432,
                protocol="tcp",
            ),
        ],
        forbidden_connections=[
            ConnectionRule(
                id="frontend_to_database",
                name="frontend_to_database",
                source="frontend",
                destination="database",
                port=5432,
                protocol="tcp",
                description="Frontend must not access DB directly",
            ),
            ConnectionRule(
                id="client_to_database",
                name="client_to_database",
                source="client",
                destination="database",
                port=5432,
                protocol="tcp",
                description="Public clients must not access DB",
            ),
        ],
    )


# ===========================================================================
# 1. Mandatory Invariant Test: Verifier does NOT import executor
# ===========================================================================


def test_verifier_modules_do_not_import_executor() -> None:
    """AST inspection verifying IndependentVerifier and contract modules never import executor."""
    verification_dir = Path(__file__).resolve().parents[1] / "core" / "verification"
    assert verification_dir.is_dir()

    python_files = list(verification_dir.glob("*.py"))
    assert len(python_files) >= 3

    forbidden_patterns = ["executor", "safe_executor", "safe_action_live", "execution_agent"]

    for py_file in python_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for pattern in forbidden_patterns:
                        assert pattern not in alias.name.lower(), (
                            f"Violation in {py_file.name}: verifier module imports '{alias.name}', "
                            f"which contains forbidden executor reference '{pattern}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                for pattern in forbidden_patterns:
                    assert pattern not in module_name.lower(), (
                        f"Violation in {py_file.name}: verifier module imports from '{module_name}', "
                        f"which contains forbidden executor reference '{pattern}'"
                    )
                for alias in node.names:
                    for pattern in forbidden_patterns:
                        assert pattern not in alias.name.lower(), (
                            f"Violation in {py_file.name}: verifier module imports symbol '{alias.name}' "
                            f"from '{module_name}', containing forbidden executor reference '{pattern}'"
                        )


# ===========================================================================
# 2. Mandatory Test: Divergence case (Health OK, Contract FAILS -> RQ2 metric)
# ===========================================================================


def test_divergence_case_health_ok_contract_fails() -> None:
    """Health check passes, but allowed connection flow fails.

    Hard rule: verdict MUST NOT be RESTORED.
    Divergence metric counter must be incremented and saved to file for RQ2.
    """
    with TemporaryDirectory() as tmp_dir:
        metrics_file = Path(tmp_dir) / "rq2_metrics.json"
        tracker = RQ2DivergenceTracker(metrics_file=metrics_file)
        contract = _sample_demo_contract()

        verifier = IndependentVerifier(
            contract=contract,
            n_consecutive_checks=1,
            divergence_tracker=tracker,
        )

        # Health probe passes for all services (frontend, backend, database)
        def health_probe(svc: str) -> tuple[bool, str]:
            return True, f"{svc} healthz 200 OK"

        # Allowed connection frontend_to_backend fails (connection refused / API down)
        def allowed_probe(conn: ConnectionRule) -> tuple[bool, str]:
            if conn.id == "frontend_to_backend":
                return False, "Connection refused on port 8080"
            return True, "Connected"

        # Forbidden connections are properly blocked
        def forbidden_probe(conn: ConnectionRule) -> tuple[bool, str]:
            return True, "Blocked (connection refused)"

        report = verifier.verify(
            incident_id="inc-div-01",
            scenario_id="NET-02",
            health_probe_fn=health_probe,
            allowed_probe_fn=allowed_probe,
            forbidden_probe_fn=forbidden_probe,
        )

        # Invariants:
        assert report.health_passed is True, "Health check must be observed as passed"
        assert report.contract_passed is False, "Contract check must fail due to broken flow"
        assert report.verdict == VerifierVerdict.NOT_RESTORED, "Hard rule: cannot be RESTORED"
        assert report.is_divergence_case is True, "Must be flagged as a divergence case"
        assert report.action_chain_stopped is True

        # Verify RQ2 metric tracking:
        assert tracker.divergence_count == 1
        assert tracker.total_evaluations == 1

        # Verify file persistence:
        assert metrics_file.is_file()
        persisted = json.loads(metrics_file.read_text(encoding="utf-8"))
        assert persisted["divergence_count"] == 1
        assert len(persisted["records"]) == 1
        assert persisted["records"][0]["incident_id"] == "inc-div-01"
        assert persisted["records"][0]["health_passed"] is True
        assert persisted["records"][0]["contract_passed"] is False


# ===========================================================================
# 3. Mandatory Test: Forbidden flow open (security regression) -> NOT_RESTORED
# ===========================================================================


def test_forbidden_flow_open_causes_not_restored() -> None:
    """All allowed flows pass and health passes, but a forbidden flow is accessible.

    The system appears operational, but security isolation is violated.
    Verdict must be NOT_RESTORED.
    """
    contract = _sample_demo_contract()
    verifier = IndependentVerifier(contract=contract, n_consecutive_checks=1)

    # Health check passes
    def health_probe(svc: str) -> tuple[bool, str]:
        return True, "OK"

    # All allowed connections pass
    def allowed_probe(conn: ConnectionRule) -> tuple[bool, str]:
        return True, "Connected"

    # Forbidden probe: frontend_to_database is OPEN (vulnerable!)
    def forbidden_probe(conn: ConnectionRule) -> tuple[bool, str]:
        if conn.id == "frontend_to_database":
            # Returning (False, ...) means the connection is NOT blocked (i.e. connection succeeded!)
            return False, "VULNERABILITY DETECTED: port 5432 directly accessible from frontend"
        return True, "Blocked as expected"

    report = verifier.verify(
        incident_id="inc-sec-01",
        scenario_id="SEC-01",
        health_probe_fn=health_probe,
        allowed_probe_fn=allowed_probe,
        forbidden_probe_fn=forbidden_probe,
    )

    assert report.health_passed is True
    assert report.contract_passed is False
    assert report.verdict == VerifierVerdict.NOT_RESTORED
    assert "Forbidden frontend_to_database unblocked" in report.reason
    assert report.action_chain_stopped is True


# ===========================================================================
# 4. Mandatory Test: N consecutive stability checks
# ===========================================================================


def test_restored_requires_n_consecutive_successful_checks() -> None:
    """RESTORED requires N consecutive successful passes.

    If any round fails in between, the streak resets.
    """
    contract = _sample_demo_contract()
    call_counts = {"round": 0}

    # Round 1: PASS, Round 2: FAIL, Round 3: PASS -> Should NOT reach N=3 consecutive
    def flaking_scenario_probe(scenario_id: str) -> tuple[bool, str]:
        call_counts["round"] += 1
        if call_counts["round"] == 2:
            return False, "Transient timeout during scenario transaction"
        return True, "Transaction completed"

    verifier = IndependentVerifier(
        contract=contract,
        n_consecutive_checks=3,
        interval_seconds=0.0,
    )

    report = verifier.verify(
        incident_id="inc-stability-01",
        scenario_id="DB-01",
        scenario_probe_fn=flaking_scenario_probe,
    )

    # Round 2 failed, streak was broken
    assert report.verdict == VerifierVerdict.NOT_RESTORED
    assert report.consecutive_successful_rounds < 3


def test_restored_succeeds_when_all_n_consecutive_checks_pass() -> None:
    """All N rounds pass consecutively -> verdict is RESTORED."""
    contract = _sample_demo_contract()
    verifier = IndependentVerifier(
        contract=contract,
        n_consecutive_checks=3,
        interval_seconds=0.0,
    )

    report = verifier.verify(
        incident_id="inc-stable-02",
        scenario_id="DB-01",
    )

    assert report.verdict == VerifierVerdict.RESTORED
    assert report.consecutive_successful_rounds == 3
    assert report.total_rounds_run == 3
    assert report.health_passed is True
    assert report.contract_passed is True
    assert report.action_chain_stopped is False


# ===========================================================================
# 5. Mandatory Test: NOT_RESTORED stops action chain, audits, alerts operator
# ===========================================================================


def test_not_restored_stops_chain_writes_audit_and_notifies_operator() -> None:
    """NOT_RESTORED halts action chain, writes audit log, alerts operator, no auto-rollback."""
    with TemporaryDirectory() as tmp_dir:
        audit_file = Path(tmp_dir) / "verifier_audit.jsonl"
        audit_logger = SafetyAuditLogger(log_path=audit_file)
        contract = _sample_demo_contract()

        notifications: list[str] = []

        class MockNotifier:
            def send_telegram_message(self, text: str) -> bool:
                notifications.append(text)
                return True

        verifier = IndependentVerifier(
            contract=contract,
            n_consecutive_checks=1,
            audit_logger=audit_logger,
            notifier=MockNotifier(),
        )

        # Health fails
        def health_probe(svc: str) -> tuple[bool, str]:
            return False, "Service crash loop"

        report = verifier.verify(
            incident_id="inc-halt-01",
            scenario_id="CON-01",
            plan_hash="p" * 64,
            health_probe_fn=health_probe,
        )

        assert report.verdict == VerifierVerdict.NOT_RESTORED
        assert report.action_chain_stopped is True
        assert report.operator_notified is True
        assert report.auto_rollback_triggered is False
        assert len(notifications) == 1
        assert "VERIFICATION ALERT" in notifications[0]

        # Verify audit line written
        lines = [l.strip() for l in audit_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1
        audit_entry = json.loads(lines[0])
        assert audit_entry["result"] == "ABORTED"
        assert audit_entry["reason_code"] == "VERIFIER_NOT_RESTORED"


# ===========================================================================
# 6. Sample Demo-Web YAML Contract Parsing Test
# ===========================================================================


def test_sample_demo_web_contract_yaml_structure() -> None:
    """Verify demo-web YAML communication contract format and validation."""
    assert SAMPLE_YAML_PATH.is_file(), f"Sample contract YAML missing at {SAMPLE_YAML_PATH}"

    contract = load_communication_contract(SAMPLE_YAML_PATH)
    assert contract.version == "1.0"
    assert contract.contract_name == "demo-web-communication-contract"
    assert contract.sample_only is True
    assert contract.target_system == "demo-web"

    # Services
    assert set(contract.services.keys()) == {"frontend", "backend", "database"}
    assert "frontend_to_backend" in contract.services["frontend"].required_flows
    assert "backend_to_database" in contract.services["backend"].required_flows

    # Connections
    allowed_ids = {c.id for c in contract.allowed_connections}
    assert "frontend_to_backend" in allowed_ids
    assert "backend_to_database" in allowed_ids

    forbidden_ids = {c.id for c in contract.forbidden_connections}
    assert "frontend_to_database" in forbidden_ids
    assert "client_to_database" in forbidden_ids
    assert "database_to_internet" in forbidden_ids


def test_rq2_divergence_tracker_auto_saves_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    metrics_file = tmp_path / "rq2_metrics.json"
    monkeypatch.setenv("AIOPS_RQ2_METRICS_FILE", str(metrics_file))

    tracker = RQ2DivergenceTracker()
    assert tracker._metrics_file == metrics_file

    tracker.record_evaluation(
        incident_id="inc-env-test",
        scenario_id="DB-01",
        health_passed=True,
        contract_passed=False,
        verdict=VerifierVerdict.NOT_RESTORED,
    )

    assert metrics_file.is_file()
    data = json.loads(metrics_file.read_text(encoding="utf-8"))
    assert data["divergence_count"] == 1
    assert data["total_evaluations"] == 1
    assert data["records"][0]["incident_id"] == "inc-env-test"


def test_load_moodle_communication_contract() -> None:
    """Validate that the official Moodle infrastructure communication contract loads and matches schema."""
    moodle_yaml_path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "contracts"
        / "moodle_communication_contract.yaml"
    )
    assert moodle_yaml_path.is_file(), f"Moodle contract not found at {moodle_yaml_path}"

    contract = load_communication_contract(moodle_yaml_path)
    assert contract.target_system == "moodle"
    assert contract.sample_only is False
    assert contract.version == "1.0"
    assert contract.contract_name == "moodle-infrastructure-communication-contract"

    # Services verification
    expected_services = {"alb", "moodle-app-01", "moodle-app-02", "rds", "efs", "monitoring"}
    assert set(contract.services.keys()) == expected_services
    assert "alb_to_app_01" in contract.services["alb"].required_flows
    assert "alb_to_app_02" in contract.services["alb"].required_flows
    assert "app_01_to_rds" in contract.services["moodle-app-01"].required_flows
    assert "app_01_to_efs" in contract.services["moodle-app-01"].required_flows
    assert "app_02_to_rds" in contract.services["moodle-app-02"].required_flows
    assert "app_02_to_efs" in contract.services["moodle-app-02"].required_flows

    # Connections verification
    allowed_ids = {c.id for c in contract.allowed_connections}
    assert "client_to_alb" in allowed_ids
    assert "alb_to_app_01" in allowed_ids
    assert "alb_to_app_02" in allowed_ids
    assert "app_01_to_rds" in allowed_ids
    assert "app_02_to_rds" in allowed_ids
    assert "app_01_to_efs" in allowed_ids
    assert "app_02_to_efs" in allowed_ids
    assert "monitoring_to_alb" in allowed_ids

    forbidden_ids = {c.id for c in contract.forbidden_connections}
    assert "client_to_rds_forbidden" in forbidden_ids
    assert "client_to_efs_forbidden" in forbidden_ids
    assert "rds_to_internet_forbidden" in forbidden_ids
    assert "efs_to_internet_forbidden" in forbidden_ids
    assert "client_to_app_direct_forbidden" in forbidden_ids



