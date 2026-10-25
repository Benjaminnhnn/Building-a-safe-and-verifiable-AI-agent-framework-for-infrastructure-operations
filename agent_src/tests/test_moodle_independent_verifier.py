"""Operator verifier must bind live probes to the executed audit receipt."""

from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

VERIFIER_PATH = Path(__file__).resolve().parents[2] / "automation" / "moodle-independent-verifier.py"
spec = importlib.util.spec_from_file_location("moodle_independent_verifier", VERIFIER_PATH)
assert spec is not None and spec.loader is not None
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
EXPORT_PATH = Path(__file__).resolve().parents[2] / "automation" / "export-moodle-safe-action-audit.py"
export_spec = importlib.util.spec_from_file_location("export_moodle_safe_action_audit", EXPORT_PATH)
assert export_spec is not None and export_spec.loader is not None
audit_export = importlib.util.module_from_spec(export_spec)
export_spec.loader.exec_module(audit_export)


def _audit_db(path: Path, request_id: str = "req-1") -> None:
    with verifier.SafeActionAuditStore(path) as audit:
        audit.append(
            incident_id="inc-1",
            event_type="live_executor_result",
            payload={
                "request_id": request_id,
                "scenario_id": "DB-01",
                "status": "executed",
                "mutated": True,
            },
        )


def _private_key(path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return path


def test_verifier_requires_unique_executed_audit_receipt(tmp_path: Path) -> None:
    audit_db = tmp_path / "audit.sqlite"
    _audit_db(audit_db)
    before = audit_db.read_bytes()
    assert verifier._audit_receipt(audit_db, "inc-1", "req-1", "DB-01")["payload"]["mutated"] is True
    assert audit_db.read_bytes() == before
    with pytest.raises(ValueError, match="no unique executed"):
        verifier._audit_receipt(audit_db, "inc-1", "other-request", "DB-01")


def test_live_contracts_match_all_reviewed_staging_verification_scenarios() -> None:
    assert set(verifier._SCENARIO_RELATED_CHECKS) == {
        "DB-01", "DB-02", "DB-03", "RES-01", "RES-02", "RES-03",
        "NET-01", "NET-02", "NET-03", "CON-01", "CON-02", "CON-03",
        "SEC-02", "SEC-03", "BIZ-01", "BIZ-02",
    }
    assert "SEC-01" not in verifier._ALL_VERIFIED_SCENARIOS
    for scenario_id in verifier._SCENARIO_RELATED_CHECKS:
        contract = verifier._contract_for_scenario(scenario_id)
        assert contract["allowed"] == ["common_synthetic_transaction"]
        assert contract["forbidden"] == ["internet_to_postgresql_blocked"]
        assert "scenario_alert_resolved" in contract["related"]


def test_failed_probe_preserves_json_failure_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    check = {"id": "internet_to_postgresql_blocked", "status": "failed"}
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: verifier.subprocess.CompletedProcess(
            [], 1, stdout=json.dumps({
                "scenario_id": "DB-01", "drill_id": "req-1", "status": "failed", "checks": [check],
            }), stderr="",
        ),
    )
    assert verifier._run_probe("DB-01", "req-1") == {
        "internet_to_postgresql_blocked": False
    }


def test_probe_invokes_only_the_reviewed_script_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    payload = {
        "scenario_id": "DB-01", "drill_id": "req-1", "status": "passed",
        "checks": [{"id": "one", "status": "passed"}],
    }

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return verifier.subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    assert verifier._run_probe("DB-01", "req-1") == {"one": True}
    assert captured["command"] == ["bash", str(verifier._PROBE_SCRIPT), "DB-01", "req-1"]
    assert captured["kwargs"]["shell"] is False


@pytest.mark.parametrize(
    "returncode,status,check_status",
    [(0, "passed", "failed"), (1, "failed", "passed"), (0, "failed", "passed")],
)
def test_probe_rejects_conflicting_exit_overall_and_check_status(
    monkeypatch: pytest.MonkeyPatch, returncode: int, status: str, check_status: str,
) -> None:
    payload = {
        "scenario_id": "DB-01", "drill_id": "req-1", "status": status,
        "checks": [{"id": "one", "status": check_status}],
    }
    monkeypatch.setattr(
        verifier.subprocess, "run",
        lambda command, **_kwargs: verifier.subprocess.CompletedProcess(
            command, returncode, stdout=json.dumps(payload), stderr="",
        ),
    )
    with pytest.raises(ValueError, match="exit status"):
        verifier._run_probe("DB-01", "req-1")


@pytest.mark.parametrize("field,value", [("scenario_id", "DB-02"), ("drill_id", "other-request")])
def test_probe_rejects_output_bound_to_another_request(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str,
) -> None:
    payload = {
        "scenario_id": "DB-01", "drill_id": "req-1", "status": "passed",
        "checks": [{"id": "one", "status": "passed"}],
    }
    payload[field] = value
    monkeypatch.setattr(
        verifier.subprocess, "run",
        lambda command, **_kwargs: verifier.subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr="",
        ),
    )
    with pytest.raises(ValueError, match="not bound"):
        verifier._run_probe("DB-01", "req-1")


def test_cli_rejects_caller_supplied_probe_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys, "argv",
        [
            str(VERIFIER_PATH), "--audit-db", "unused.sqlite", "--incident-id", "inc-1",
            "--request-id", "req-1", "--scenario-id", "DB-01", "--",
            sys.executable, "-c", "raise SystemExit(0)",
        ],
    )
    monkeypatch.setattr(verifier.subprocess, "run", lambda *_a, **_k: pytest.fail("must not execute"))
    with pytest.raises(SystemExit) as exc_info:
        verifier.main()
    assert exc_info.value.code == 2


def test_audit_export_uses_consistent_read_only_snapshot_and_protects_file(
    tmp_path: Path,
) -> None:
    ssh_config = tmp_path / "ssh.config"
    ssh_config.write_text("Host monitor-ai-01\n", encoding="utf-8")
    output = tmp_path / "audit.sqlite3"
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return audit_export.subprocess.CompletedProcess(
            command, 0, stdout=b"SQLite format 3\x00snapshot", stderr=b""
        )

    assert audit_export.export_snapshot(output, ssh_config, run=fake_run) == output
    assert output.read_bytes() == b"SQLite format 3\x00snapshot"
    assert captured["command"][1:3] == ["-T", "-F"]
    assert "source.backup(target)" in captured["command"][-1]
    assert captured["kwargs"]["capture_output"] is True
    with pytest.raises(FileExistsError):
        audit_export.export_snapshot(output, ssh_config, run=fake_run)


def test_verifier_fails_closed_when_probe_source_omits_contract_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_db = tmp_path / "audit.sqlite"
    _audit_db(audit_db)
    monkeypatch.setattr(
        verifier,
        "_run_probe",
        lambda *_: {"moodle_healthy": True},
    )
    contract = {"allowed": ["moodle_healthy"], "forbidden": ["internet_to_postgresql"], "related": ["targets_healthy"]}
    with pytest.raises(ValueError, match="probe contract coverage mismatch"):
        verifier.verify_window(
            audit_db=audit_db,
            signing_key=_private_key(tmp_path / "verifier.pem"),
            incident_id="inc-1",
            request_id="req-1",
            scenario_id="DB-01",
            contract=contract,
            sleep=lambda _: None,
        )


def test_successful_window_signs_result_without_resolving_the_local_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_db = tmp_path / "audit.sqlite"
    _audit_db(audit_db)
    contract = {
        "allowed": ["moodle_healthy"],
        "forbidden": ["internet_to_postgresql"],
        "related": ["targets_healthy"],
    }
    monkeypatch.setattr(
        verifier,
        "_run_probe",
        lambda *_: {"moodle_healthy": True, "internet_to_postgresql": True, "targets_healthy": True},
    )
    elapsed = [0.0]
    base = datetime.now(timezone.utc) - timedelta(seconds=120)
    result = verifier.verify_window(
        audit_db=audit_db,
        signing_key=_private_key(tmp_path / "verifier.pem"),
        incident_id="inc-1",
        request_id="req-1",
        scenario_id="DB-01",
        contract=contract,
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        monotonic=lambda: elapsed[0],
        now=lambda: base + timedelta(seconds=elapsed[0]),
    )
    assert result["verdict"] == "ready_for_runtime_acceptance"
    assert result["verification"]["resolution_eligible"] is True
    assert result["attestation"]["payload"]["receipt_event_hash"]
    assert result["attestation"]["signature"]
    with verifier.SafeActionAuditStore(audit_db) as audit:
        assert audit.verify_chain("inc-1") is True
        assert audit.get_live_incident_status("inc-1", "req-1") == "executed"
        events = audit.list_for_incident("inc-1")
    assert len(events) == 1


def test_executed_request_remains_verifying_until_a_eligible_result_is_stored(
    tmp_path: Path,
) -> None:
    audit_db = tmp_path / "audit.sqlite"
    _audit_db(audit_db)
    with verifier.SafeActionAuditStore(audit_db) as audit:
        audit.append(
            incident_id="inc-1",
            event_type="live_verification_pending",
            payload={"request_id": "req-1", "scenario_id": "DB-01", "resolution_eligible": False},
        )
        assert audit.get_live_incident_status("inc-1", "req-1") == "verifying"

