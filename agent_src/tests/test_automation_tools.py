from __future__ import annotations

import importlib.util
import json
import sqlite3
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harvester = load_script("benchmark_harvester", "automation/harvest-benchmark-results.py")
inspector = load_script("evidence_inspector", "automation/inspect_live_db.py")
trigger = load_script("local_agent_trigger", "automation/trigger_live_agent.py")
benchmark_runner = load_script("benchmark_runner", "automation/run-benchmark.py")
aiops_replay = load_script("aiops_replay", "automation/aiops-unified-replay.py")


def test_unified_replay_accepts_safe_simulated_verification_only() -> None:
    report = {
        "error": None,
        "incident_status": "verified_dry_run",
        "gate_decision": "allow",
        "resolved_by_verifier": False,
        "execution_dry_run": True,
        "execution_executed": False,
        "execution_succeeded": True,
        "verification_run": True,
        "verification_simulated": True,
        "simulated": True,
        "pipeline_mode": "offline_simulation",
        "live_verification_executed": False,
        "recovery_claim_scope": "simulated_fixture_only",
        "scenario_binding": {"execution_permitted_by_binding": False},
    }

    assert aiops_replay._is_valid_offline_result(report)
    assert not aiops_replay._has_forbidden_execution(report)


@pytest.mark.parametrize(
    "stages",
    [["observe", "triage", "diagnose"], ["observe", "triage", "diagnose", "plan"]],
)
def test_unified_replay_accepts_fail_closed_pre_dispatch_escalation(stages: list[str]) -> None:
    report = {
        "error": None,
        "incident_status": "escalated",
        "stages": stages,
        "action_type": None,
        "gate_decision": None,
        "resolved_by_verifier": False,
        "execution_dry_run": None,
        "execution_executed": False,
        "execution_succeeded": False,
        "verification_run": False,
        "verification_simulated": None,
        "simulated": True,
        "pipeline_mode": "offline_simulation",
        "live_verification_executed": False,
        "recovery_claim_scope": "simulated_fixture_only",
        "scenario_binding": {"execution_permitted_by_binding": False},
    }

    assert aiops_replay._is_valid_offline_result(report)
    assert not aiops_replay._has_forbidden_execution(report)


def test_unified_replay_rejects_escalation_after_action_or_gate() -> None:
    report = {
        "error": None,
        "incident_status": "escalated",
        "stages": ["observe", "triage", "diagnose", "plan"],
        "action_type": "remove_scoped_db_reject",
        "gate_decision": None,
        "resolved_by_verifier": False,
        "execution_dry_run": None,
        "execution_executed": False,
        "execution_succeeded": False,
        "verification_run": False,
        "verification_simulated": None,
        "simulated": True,
        "pipeline_mode": "offline_simulation",
        "live_verification_executed": False,
        "recovery_claim_scope": "simulated_fixture_only",
        "scenario_binding": {"execution_permitted_by_binding": False},
    }

    assert not aiops_replay._is_valid_offline_result(report)


@pytest.mark.parametrize(
    "field,value",
    [
        ("incident_status", "resolved"),
        ("resolved_by_verifier", True),
        ("execution_dry_run", False),
        ("simulated", False),
        ("live_verification_executed", True),
    ],
)
def test_unified_replay_rejects_live_or_resolution_claims(
    field: str, value: object
) -> None:
    report = {
        "error": None,
        "incident_status": "verified_dry_run",
        "gate_decision": "allow",
        "resolved_by_verifier": False,
        "execution_dry_run": True,
        "execution_executed": False,
        "execution_succeeded": True,
        "verification_run": True,
        "verification_simulated": True,
        "simulated": True,
        "pipeline_mode": "offline_simulation",
        "live_verification_executed": False,
        "recovery_claim_scope": "simulated_fixture_only",
        "scenario_binding": {"execution_permitted_by_binding": False},
    }
    report[field] = value

    assert not aiops_replay._is_valid_offline_result(report)
    if field in {"resolved_by_verifier", "execution_dry_run", "live_verification_executed"}:
        assert aiops_replay._has_forbidden_execution(report)


def test_harvest_labels_fixture_benchmark_as_simulated(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    record = {
        "run_id": "fixture-1",
        "scenario_id": "DB-01",
        "method": "ai_agent",
        "repetition": 1,
        "timestamps": {"t_inject": None, "t_detect": None},
    }
    (campaign / "benchmark_results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    rows = harvester.collect_campaign(campaign, "ai_agent")

    assert rows[0]["evidence_class"] == "simulated_fixture_benchmark"
    assert rows[0]["t_inject"] == ""


def test_harvest_preserves_runtime_method_and_authority(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    run = campaign / "run-1"
    run.mkdir(parents=True)
    payload = json.dumps({
        "run_id": "DB-01-1",
        "scenario_id": "DB-01",
        "status": "passed",
        "method": "ai_agent_shadow",
        "evidence_class": "staging_runtime_shadow",
        "ai_execution": False,
        "execution_authority": "allowlisted_test_harness",
        "t_inject": "2026-10-04T00:00:00Z",
        "mttd_seconds": 7,
    })
    (run / "result.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    row = harvester.collect_campaign(campaign, "ai_agent_shadow")[0]

    assert row["evidence_class"] == "staging_runtime_shadow"
    assert row["method"] == "ai_agent_shadow"
    assert row["ai_execution"] is False
    assert row["execution_authority"] == "allowlisted_test_harness"
    assert row["t_inject"] == "2026-10-04T00:00:00Z"
    assert row["mttd_seconds"] == 7


def test_harvest_refuses_unmarked_runtime_result(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "result.json").write_text(json.dumps({
        "run_id": "run-1", "scenario_id": "DB-01", "status": "passed"
    }), encoding="utf-8")

    with pytest.raises(harvester.InputError, match="evidence_class"):
        harvester.collect_campaign(campaign, None)


def test_inspector_opens_read_only_and_emits_counts(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER)")
        connection.execute("INSERT INTO evidence VALUES (1)")
        connection.execute("CREATE TABLE checkpoints (id INTEGER)")

    assert inspector.inspect(database) == {"evidence": 1, "checkpoints": 0}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1


def test_trigger_only_allows_plain_loopback_webhook() -> None:
    assert trigger.loopback_webhook("http://127.0.0.1:8000/webhook")
    assert trigger.loopback_webhook("http://[::1]:8000/webhook")
    assert not trigger.loopback_webhook("https://127.0.0.1:8000/webhook")
    assert not trigger.loopback_webhook("http://example.com/webhook")
    assert not trigger.loopback_webhook("http://user:pass@localhost/webhook")
    assert not trigger.loopback_webhook("http://localhost/webhook/other")


def test_safe_executor_compensation_ids_match_operator_approval_and_forced_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_key = tmp_path / "agent-hmac.key"
    agent_key.write_bytes(b"test-key-with-more-than-32-bytes-long")
    monkeypatch.setenv("EXECUTOR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("EXECUTOR_SSH_KEY", str(tmp_path / "id_ed25519"))
    monkeypatch.setenv("EXECUTOR_KNOWN_HOSTS", str(tmp_path / "known_hosts"))
    monkeypatch.setenv("EXECUTOR_AGENT_KEY_FILE", str(agent_key))
    monkeypatch.setenv("EXECUTOR_APPROVAL_PUBLIC_KEY_FILE", str(tmp_path / "approval.pub"))
    monkeypatch.setenv("MOODLE_NODE_A_IP", "192.0.2.10")
    monkeypatch.setenv("MOODLE_NODE_B_IP", "192.0.2.11")

    api = load_script("safe_executor_api_allowlist", "automation/moodle-safe-executor-api.py")
    approval = load_script("safe_executor_approval_allowlist", "automation/moodle-safe-approval.py")
    expected = {
        "DB-01": {
            "remove_scoped_db_reject": ("staging_moodle_nodes", ("a", "b")),
            "reconcile_db01_safe_baseline": ("staging_moodle_nodes", ("a", "b")),
        },
        "RES-01": {
            "remove_named_cpu_load_container": ("moodle-app-b", ("b",)),
            "reconcile_res01_safe_baseline": ("moodle-app-b", ("b",)),
        },
        "NET-01": {
            "recreate_moodle_web_from_reviewed_compose": ("staging_moodle_nodes", ("a", "b")),
            "reconcile_net01_safe_baseline": ("staging_moodle_nodes", ("a", "b")),
        },
        "CON-01": {
            "start_reviewed_compose_service": ("moodle-app-b", ("b",)),
            "reconcile_con01_safe_baseline": ("moodle-app-b", ("b",)),
        },
        "SEC-02": {
            "restore_fixture_directory_mode": ("moodledata_synthetic_fixture_directory", ("a",)),
            "reconcile_sec02_safe_baseline": ("moodledata_synthetic_fixture_directory", ("a",)),
        },
    }
    assert api.CATALOG == {
        (scenario, action_id, scope): nodes
        for scenario, actions in expected.items()
        for action_id, (scope, nodes) in actions.items()
    }
    assert approval.CATALOG == {
        scenario: {action_id: scope for action_id, (scope, _) in actions.items()}
        for scenario, actions in expected.items()
    }

    forced_command = (ROOT / "ansible/playbooks/configure-moodle-safe-executor.yml").read_text(
        encoding="utf-8"
    )
    api_source = (ROOT / "automation/moodle-safe-executor-api.py").read_text(encoding="utf-8")
    assert 'f"aiops-executor@{NODES[node]}", action_id,' in api_source
    for scenario, actions in expected.items():
        assert f"{scenario}|" in forced_command
        for action_id in actions:
            assert action_id in forced_command
    assert "rules=$(iptables -S DOCKER-USER)" in forced_command
    assert "DB-01 tagged rule exists but its ownership marker is missing" in forced_command
    assert 'containers=$(docker ps -a --format' in forced_command
    assert 'docker rm -f moodle-fault-res-01 >/dev/null' in forced_command
    assert 'docker rm -f moodle-fault-res-01 >/dev/null 2>&1 || true' not in forced_command


def test_signed_compensation_approval_reaches_executor_kill_switch_without_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL is required to exercise the production Ed25519 approval verifier")
    agent_key = tmp_path / "agent-hmac.key"
    agent_key.write_bytes(b"test-key-with-more-than-32-bytes-long")
    private_key = tmp_path / "operator-ed25519.pem"
    public_key = tmp_path / "operator-ed25519.pub"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("EXECUTOR_STATE_DB", str(tmp_path / "executor-state.db"))
    monkeypatch.setenv("EXECUTOR_SSH_KEY", str(tmp_path / "executor-ssh-key"))
    monkeypatch.setenv("EXECUTOR_KNOWN_HOSTS", str(tmp_path / "known_hosts"))
    monkeypatch.setenv("EXECUTOR_AGENT_KEY_FILE", str(agent_key))
    monkeypatch.setenv("EXECUTOR_APPROVAL_PUBLIC_KEY_FILE", str(public_key))
    monkeypatch.setenv("MOODLE_NODE_A_IP", "192.0.2.10")
    monkeypatch.setenv("MOODLE_NODE_B_IP", "192.0.2.11")
    monkeypatch.setenv("EXECUTOR_LIVE_ENABLED", "false")

    api = load_script("safe_executor_api_disabled_integration", "automation/moodle-safe-executor-api.py")
    api.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        approval_script = load_script(
            "safe_executor_approval_disabled_integration", "automation/moodle-safe-approval.py"
        )
        idempotency_key = "reconcile-DB-01-run-0001"
        monkeypatch.setattr(sys, "argv", [
            "moodle-safe-approval.py",
            "DB-01",
            "--action-id",
            "reconcile_db01_safe_baseline",
            "--private-key",
            str(private_key),
            "--idempotency-key",
            idempotency_key,
            "--actor",
            "operator",
        ])
        assert approval_script.main() == 0
        signed_approval = json.loads(capsys.readouterr().out)

        monkeypatch.setenv(
            "SAFE_EXECUTOR_URL", f"http://127.0.0.1:{server.server_address[1]}"
        )
        monkeypatch.setenv("SAFE_EXECUTOR_HMAC_KEY_FILE", str(agent_key))
        from core.safety.safe_executor_client import execute_approved_staging_action

        result = execute_approved_staging_action(
            scenario_id=signed_approval["scenario_id"],
            catalog_action_id=signed_approval["catalog_action_id"],
            target_scope=signed_approval["target_scope"],
            idempotency_key=idempotency_key,
            approval=signed_approval["approval"],
        )

        assert result["status"] == "disabled"
        assert result["mutated"] is False
        assert result["completed_nodes"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_repeated_simulated_campaigns_get_distinct_run_ids() -> None:
    from datetime import datetime, timezone
    import random

    scenario = {"scenario_id": "DB-01"}
    base_time = datetime(2026, 10, 4, tzinfo=timezone.utc)
    first = benchmark_runner.simulate_manual_run(scenario, 1, base_time, random.Random(42), "campaign-a")
    second = benchmark_runner.simulate_manual_run(scenario, 1, base_time, random.Random(42), "campaign-b")

    assert first.run_id != second.run_id


def test_benchmark_runner_requires_explicit_synthetic_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["run-benchmark.py"])

    with pytest.raises(SystemExit) as exc:
        benchmark_runner.main()

    assert exc.value.code == 2


def test_benchmark_cli_can_include_erpnext_explicitly() -> None:
    parser = benchmark_runner.build_argument_parser()

    assert parser.parse_args([]).moodle_only is True
    assert parser.parse_args(["--include-erpnext"]).moodle_only is False
    assert parser.parse_args(["--moodle-only"]).moodle_only is True
