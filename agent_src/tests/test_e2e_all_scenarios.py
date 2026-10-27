"""Detect -> analyse -> propose -> approve -> execute -> verify, for every scenario.

``test_end_to_end_incident_lifecycle`` walks this chain for CON-01 and DB-01.
This file runs the same chain for each agent-executable thesis scenario, from an
Alertmanager payload to a signed verifier attestation, with Prometheus replaced
by the live-shaped samples of ``test_live_shaped_planning``. SEC-01 must stop at
a human-only escalation and never yield a candidate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.evidence.evidence_collectors import EvidenceDraft
from core.evidence.evidence_store import SQLiteEvidenceStore
from core.moodle.alert_integration import process_moodle_alert
from core.moodle.live_handoff import execute_moodle_live_handoff, prepare_moodle_live_handoff
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_execution_gate import ApprovalRecord
from core.safety.verifier_attestation import accept_verifier_attestation, get_runtime_verification_status
from test_end_to_end_incident_lifecycle import (
    _build_signed_attestation,
    _generate_ed25519_pair,
    _sign_executor_approval,
)
from test_live_shaped_planning import LIVE_SHAPES

# Catalog action the agent must propose for each scenario.
EXPECTED_ACTIONS = {
    "BIZ-01": "disable_moodle_cli_maintenance_mode",
    "BIZ-02": "restart_moodle_cron_service",
    "CON-01": "start_reviewed_compose_service",
    "DB-01": "remove_scoped_db_reject",
    "RES-01": "remove_named_cpu_load_container",
    "NET-01": "recreate_moodle_web_from_reviewed_compose",
}


# Nodes the host executor reports per target scope.
NODES_BY_SCOPE = {
    "staging_moodle_nodes": ["a", "b"],
    "moodle-app-b": ["b"],
    "moodledata_synthetic_fixture_directory": ["a"],
    "staging_moodle_app": ["a"],
}


def _alert(alert_name: str) -> dict[str, Any]:
    return {
        "status": "firing",
        "fingerprint": f"fp-e2e-{alert_name}",
        "startsAt": "2026-10-27T10:00:00Z",
        "labels": {"alertname": alert_name, "service": "moodle", "environment": "staging"},
        "annotations": {"summary": f"{alert_name} firing"},
    }


def _mock_prometheus(monkeypatch: Any, values: dict[str, dict[str, float]]) -> None:
    def collect(_self: Any, query_id: str, _expression: str, resource_id: str) -> list[EvidenceDraft]:
        return [
            EvidenceDraft(
                "prometheus", resource_id, "metric_sample", datetime.now(timezone.utc),
                {"query_id": query_id, "sample_value": float(value), "labels": {"instance": instance}},
            )
            for instance, value in values.get(query_id, {}).items()
        ]

    monkeypatch.setattr(
        "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query", collect
    )


def _configure(monkeypatch: Any, evidence_db: Path) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(evidence_db))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.delenv("VECTOR_DB_PATH", raising=False)


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED_ACTIONS))
def test_alert_to_resolved_for_every_agent_executable_scenario(
    scenario_id: str, tmp_path: Path, monkeypatch: Any
) -> None:
    alert_name, values = LIVE_SHAPES[scenario_id]
    evidence_db, audit_db = tmp_path / "evidence.sqlite3", tmp_path / "audit.sqlite3"
    _configure(monkeypatch, evidence_db)
    _mock_prometheus(monkeypatch, values)

    # 1. Detect + analyse + propose: the agent finds the one allowlisted fix.
    report = process_moodle_alert(_alert(alert_name))
    assert report["status"] == "remediation_candidate", report.get("diagnosis")
    assert report["execution_permitted"] is False and report["resolution_eligible"] is False
    assert report["approval_required"] is True
    candidate = report["remediation_candidate"]
    assert candidate["scenario_id"] == scenario_id
    assert candidate["catalog_action_id"] == EXPECTED_ACTIONS[scenario_id]
    incident_id = report["incident_id"]

    with SQLiteEvidenceStore(evidence_db) as store:
        candidate_id = next(
            item.evidence_id for item in store.query(incident_id=incident_id, source="ai_decision")
            if item.metadata["decision_kind"] == "remediation_candidate"
        )

    # 2. Prepare for the admin: nothing runs, two digests await signatures.
    _, gate_priv_pem, gate_pub_pem = _generate_ed25519_pair()
    prepared = prepare_moodle_live_handoff(
        evidence_db=evidence_db, candidate_id=candidate_id,
        gate_public_key=gate_pub_pem, gate_actors={"safety-officer"},
    )
    assert prepared["status"] == "approval_required" and prepared["execution_permitted"] is False
    request_id = prepared["request"]["request_id"]
    calls: list[dict[str, Any]] = []

    def fake_executor(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        nodes = NODES_BY_SCOPE[kwargs["target_scope"]]
        return {"status": "executed", "scenario_id": kwargs["scenario_id"], "completed_nodes": nodes, "mutated": True}

    def run(gate_approval: Any, executor_approval: Any) -> dict[str, Any]:
        return execute_moodle_live_handoff(
            evidence_db=evidence_db, candidate_id=candidate_id, request_data=prepared["request"],
            gate_approval=gate_approval, executor_approval=executor_approval,
            gate_public_key=gate_pub_pem, gate_actors={"safety-officer"},
            audit_db=audit_db, kill_switch_enabled=False, executor=fake_executor,
        )

    gate_approval = ApprovalRecord.create_ed25519(
        action_sha256=prepared["gate_action_sha256"], actor_id="safety-officer", private_key_pem=gate_priv_pem
    )
    exec_priv, _, _ = _generate_ed25519_pair()
    executor_approval = _sign_executor_approval(prepared["executor_action_sha256"], exec_priv)

    # 3. Without both approvals the executor is never called.
    first = run(None, None)
    assert first["status"] == "awaiting_approval", first.get("reason")
    assert run(gate_approval, None)["status"] == "awaiting_approval"
    assert calls == []

    # 4. With both approvals it executes once, then waits for the verifier.
    result = run(gate_approval, executor_approval)
    assert result["status"] == "awaiting_verification" and result["resolution_eligible"] is False
    assert len(calls) == 1
    assert (calls[0]["scenario_id"], calls[0]["catalog_action_id"]) == (scenario_id, EXPECTED_ACTIONS[scenario_id])
    assert get_runtime_verification_status(
        audit_db=audit_db, incident_id=incident_id, request_id=request_id
    )["status"] == "verifying"

    # 5. Only a signed Independent Verifier attestation resolves the incident.
    with SafeActionAuditStore(audit_db) as audit:
        receipt_hash = next(
            e["event_hash"] for e in audit.list_for_incident(incident_id) if e["event_type"] == "live_executor_result"
        )
    verifier_priv, _, verifier_pub_pem = _generate_ed25519_pair()
    verifier_key = tmp_path / "verifier-public.pem"
    verifier_key.write_bytes(verifier_pub_pem)
    verified = accept_verifier_attestation(
        audit_db=audit_db, public_key_path=verifier_key, request_id=request_id,
        attestation=_build_signed_attestation(
            incident_id=incident_id, request_id=request_id, scenario_id=scenario_id,
            receipt_event_hash=receipt_hash, verifier_private=verifier_priv,
        ),
    )
    assert verified["status"] == "resolved"
    with SafeActionAuditStore(audit_db) as audit:
        assert audit.verify_chain(incident_id) is True


def test_sec01_is_diagnosed_but_only_escalated_to_a_human(tmp_path: Path, monkeypatch: Any) -> None:
    _configure(monkeypatch, tmp_path / "evidence.sqlite3")
    _mock_prometheus(monkeypatch, {
        "moodle_rds_sg_unreviewed_ingress_rules": {"monitor-ai-01": 1},
        "moodle_rds_sg_public_ingress_rules": {"monitor-ai-01": 0},
        "moodle_rds_tcp_probe": {"monitor-ai-01": 1},
    })
    report = process_moodle_alert(_alert("MoodleDatabasePortExposure"))
    assert report["diagnosis"]["status"] == "hypothesis"
    assert report["remediation_candidate"] is None
    assert report["remediation_disposition"]["disposition"] == "human_only_escalation"
    assert report["execution_permitted"] is False


def test_conflicting_evidence_produces_no_candidate(tmp_path: Path, monkeypatch: Any) -> None:
    alert_name, values = LIVE_SHAPES["CON-01"]
    _configure(monkeypatch, tmp_path / "evidence.sqlite3")
    _mock_prometheus(monkeypatch, {**values, "moodle_node_web_running": {"moodle-app-a": 1, "moodle-app-b": 1}})
    report = process_moodle_alert(_alert(alert_name))
    assert report["status"] != "remediation_candidate"
    assert report["remediation_candidate"] is None
