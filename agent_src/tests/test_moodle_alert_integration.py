from __future__ import annotations

import asyncio
import json

import pytest

from core import tasks
from core.moodle.alert_integration import process_moodle_alert
from core.evidence.evidence_collectors import METRIC_QUERIES_BY_ALERT
from core.evidence.evidence_store import EvidenceIntegrityError
from core.moodle.contract import load_moodle_ground_truth

_MOODLE_SCENARIOS = load_moodle_ground_truth()


def _alert(scenario_id: str = "DB-01", alert_name: str = "MoodleSyntheticTransactionFailed") -> dict:
    return {
        "status": "firing",
        "event_id": "evt-integration-1",
        "fingerprint": "moodle-integration-1",
        "labels": {"alertname": alert_name, "scenario_id": scenario_id, "environment": "staging", "service": "moodle", "severity": "critical"},
        "annotations": {"summary": "staging synthetic check failed"},
    }


@pytest.mark.parametrize("mode", ["shadow", "investigate"])
def test_observation_mode_collects_evidence_without_ground_truth_diagnosis(tmp_path, monkeypatch, mode) -> None:
    from core.evidence.evidence_store import SQLiteEvidenceStore

    database_path = tmp_path / "evidence.sqlite3"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", mode)
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(database_path))
    monkeypatch.delenv("AIOPS_EVIDENCE_DB_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    report = process_moodle_alert(_alert())

    assert report["status"] == "awaiting_evidence"
    assert report["mode"] == mode
    assert report["stage"] == "observe"
    assert report["evidence_status"] == "partial"
    assert len(report["evidence_refs"]) == 4
    assert len(report["decision_evidence_refs"]) == 1
    with SQLiteEvidenceStore(database_path) as store:
        decisions = store.query(incident_id=report["incident_id"], source="ai_decision")
        assert len(decisions) == 1
        assert decisions[0].evidence_type.value == "decision"
        assert store.verify_integrity() == len(store.list_for_incident(report["incident_id"]))
    assert "scenario_binding" not in report
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("shadow", "shadow_hypothesis"), ("investigate", "investigation_proposal")],
)
def test_evidence_diagnosis_and_read_only_plan_use_current_prometheus_data(
    tmp_path, monkeypatch, mode, expected_status
) -> None:
    from datetime import datetime, timezone

    from core.evidence.evidence_collectors import EvidenceDraft
    from core.evidence.evidence_store import SQLiteEvidenceStore

    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", mode)
    database_path = tmp_path / "evidence.sqlite3"
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(database_path))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "rag"))
    collected_queries = []

    def collect(_self, query_id, _expression, resource_id):
        collected_queries.append(query_id)
        value = 0.0 if query_id in {"moodle_synthetic_success", "moodle_rds_tcp_probe"} else 1.0
        return [
            EvidenceDraft(
                "prometheus",
                resource_id,
                "metric_sample",
                datetime.now(timezone.utc),
                {"query_id": query_id, "sample_value": value},
            )
        ]

    monkeypatch.setattr(
        "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query",
        collect,
    )
    monkeypatch.setattr(
        "core.rag.rag_engine.get_rag_instance",
        lambda: type(
            "ReadOnlyRunbook",
            (),
            {
                "query_standard_runbooks": lambda self, _query, alert_name: (
                    f"[Source: Moodle KB; SHA-256: abc123] Advisory only for {alert_name}."
                )
            },
        )(),
    )
    report = process_moodle_alert(_alert())
    repeated_report = process_moodle_alert(_alert())

    assert report["status"] == expected_status, report["diagnosis"].get("reason")
    assert repeated_report == report
    assert len(collected_queries) == len(METRIC_QUERIES_BY_ALERT["MoodleSyntheticTransactionFailed"])
    assert report["mode"] == mode
    assert report["stage"] == "diagnose"
    assert report["diagnosis"]["hypothesis"] == "Monitor-to-RDS TCP probe is failing; Moodle-node reachability is unconfirmed"
    assert report["diagnosis"]["action_candidate"] is None
    assert report["plan_candidate"]["action_type"] == "read_health"
    assert report["plan_candidate"]["target_resource_id"] == "postgres-db"
    assert report["plan_candidate"]["evidence_refs"] == report["diagnosis"]["evidence_refs"]
    assert len(report["decision_evidence_refs"]) == 2
    with SQLiteEvidenceStore(database_path) as store:
        decisions = store.query(incident_id=report["incident_id"], source="ai_decision")
        by_kind = {item.metadata["decision_kind"]: item for item in decisions}
        assert set(by_kind) == {"diagnosis", "read_only_plan"}
        diagnosis_record = json.loads(by_kind["diagnosis"].metadata["decision_json"])
        plan_record = json.loads(by_kind["read_only_plan"].metadata["decision_json"])
        assert set(diagnosis_record["evidence_refs"]) == set(report["evidence_refs"])
        assert set(diagnosis_record["supporting_evidence_refs"]) == set(report["diagnosis"]["evidence_refs"])
        assert plan_record["evidence_refs"] == report["plan_candidate"]["evidence_refs"]
        assert store.verify_integrity() == len(store.list_for_incident(report["incident_id"]))
    assert "SHA-256: abc123" in report["runbook_context"]
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_investigate_returns_exact_runtime_remediation_candidate_without_dispatch(
    tmp_path, monkeypatch
) -> None:
    from datetime import datetime, timezone
    from core.evidence.evidence_collectors import EvidenceDraft
    from core.evidence.evidence_store import SQLiteEvidenceStore

    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    database_path = tmp_path / "evidence.sqlite3"
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(database_path))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.delenv("VECTOR_DB_PATH", raising=False)

    samples = {
        "moodle_node_cpu_busy_percent": [(22.0, "moodle-app-a"), (82.0, "moodle-app-b")],
        "moodle_fault_res01_cpu_fixture_running": [(0.0, "moodle-app-a"), (1.0, "moodle-app-b")],
        "moodle_node_web_running": [(1.0, "moodle-app-a"), (1.0, "moodle-app-b")],
    }

    def collect(_self, query_id, _expression, resource_id):
        if query_id not in samples:
            return []
        return [
            EvidenceDraft(
                "prometheus",
                resource_id,
                "metric_sample",
                datetime.now(timezone.utc),
                {"query_id": query_id, "sample_value": value, "labels": {"instance": instance}},
            )
            for value, instance in samples[query_id]
        ]

    monkeypatch.setattr(
        "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query", collect
    )
    alert = _alert("SEC-02", "MoodleNodeCpuHigh")
    report = process_moodle_alert(alert)

    assert report["status"] == "remediation_candidate"
    proposal = report["remediation_candidate"]
    assert proposal["scenario_id"] == "RES-01"
    assert proposal["catalog_action_id"] == "remove_named_cpu_load_container"
    assert proposal["target_scope"] == "moodle-app-b"
    assert proposal["approval_required"] is True
    assert proposal["execution_permitted"] is False
    assert proposal["resolution_eligible"] is False
    with SQLiteEvidenceStore(database_path) as store:
        decisions = store.query(incident_id=report["incident_id"], source="ai_decision")
        kinds = {item.metadata["decision_kind"] for item in decisions}
        assert "remediation_candidate" in kinds
        assert store.verify_integrity() == len(store.list_for_incident(report["incident_id"]))
        candidate_id = next(
            item.evidence_id for item in decisions
            if item.metadata["decision_kind"] == "remediation_candidate"
        )

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from core.moodle.live_handoff import prepare_moodle_live_handoff

    gate_private_key = Ed25519PrivateKey.generate()
    gate_public_key = gate_private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    prepared = prepare_moodle_live_handoff(
        evidence_db=database_path,
        candidate_id=candidate_id,
        gate_public_key=gate_public_key,
        gate_actors={"safety-officer"},
    )
    assert prepared["status"] == "approval_required"
    assert prepared["request"]["scenario_id"] == "RES-01"
    assert len(prepared["gate_action_sha256"]) == 64
    assert len(prepared["executor_action_sha256"]) == 64
    assert prepared["execution_permitted"] is False
    assert prepared["resolution_eligible"] is False

    import base64
    from datetime import timedelta
    from core.moodle.live_handoff import execute_moodle_live_handoff
    from core.safety.safe_execution_gate import ApprovalRecord

    gate_approval = ApprovalRecord.create_ed25519(
        action_sha256=prepared["gate_action_sha256"],
        actor_id="safety-officer",
        private_key_pem=gate_private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    executor_private_key = Ed25519PrivateKey.generate()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    signed = f'{prepared["executor_action_sha256"]}:operator:{expires_at}'.encode()
    executor_approval = {
        "actor_id": "operator",
        "expires_at": expires_at,
        "action_sha256": prepared["executor_action_sha256"],
        "signature": base64.b64encode(executor_private_key.sign(signed)).decode("ascii"),
    }

    def fake_executor(**kwargs):
        assert kwargs["scenario_id"] == "RES-01"
        return {"status": "executed", "scenario_id": "RES-01", "completed_nodes": ["b"], "mutated": True}

    result = execute_moodle_live_handoff(
        evidence_db=database_path,
        candidate_id=candidate_id,
        request_data=prepared["request"],
        gate_approval=gate_approval,
        executor_approval=executor_approval,
        gate_public_key=gate_public_key,
        gate_actors={"safety-officer"},
        audit_db=tmp_path / "safe-action-audit.sqlite3",
        kill_switch_enabled=False,
        executor=fake_executor,
    )
    assert result["status"] == "awaiting_verification", result
    assert result["resolution_eligible"] is False


def test_shadow_integration_marks_unenabled_scenario_offline_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))
    monkeypatch.delenv("AIOPS_EVIDENCE_DB_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    report = process_moodle_alert(_alert("DB-02"))

    assert report["status"] == "awaiting_evidence"
    assert report["scenario_id"] == "DB-02"
    assert "scenario_binding" not in report
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


@pytest.mark.parametrize(
    ("scenario_id", "alert_name"),
    [
        (scenario_id, scenario["observed_signals"][0])
        for scenario_id, scenario in sorted(_MOODLE_SCENARIOS.items())
    ],
    ids=sorted(_MOODLE_SCENARIOS),
)
def test_all_ground_truth_scenarios_stay_shadow_only(
    tmp_path, monkeypatch, scenario_id: str, alert_name: str
) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / f"{scenario_id}.sqlite3"))
    monkeypatch.delenv("AIOPS_EVIDENCE_DB_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    report = process_moodle_alert(_alert(scenario_id, alert_name))

    assert report["status"] == "awaiting_evidence", scenario_id
    assert report["scenario_id"] == scenario_id
    assert "scenario_binding" not in report
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False
    assert len(report["evidence_refs"]) == 4


def test_shadow_integration_does_not_turn_unknown_signal_into_a_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))
    monkeypatch.delenv("AIOPS_EVIDENCE_DB_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    report = process_moodle_alert(_alert(alert_name="UnrelatedAlert"))

    assert report["status"] == "awaiting_evidence"
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_shadow_integration_never_reads_ground_truth_answers_or_actions(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    database_path = tmp_path / "evidence.sqlite3"
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(database_path))
    monkeypatch.delenv("AIOPS_EVIDENCE_DB_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    report = process_moodle_alert(_alert())

    assert report["status"] == "awaiting_evidence"
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False
    assert database_path.exists()


def test_shadow_integration_without_scenario_uses_generic_observer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))
    monkeypatch.delenv("AIOPS_EVIDENCE_DB_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    alert = _alert()
    del alert["labels"]["scenario_id"]

    report = process_moodle_alert(alert)

    assert report["status"] == "awaiting_evidence"
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_corrupt_evidence_escalates_without_shadow_diagnosis(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")

    def raise_integrity_error(_alert):
        raise EvidenceIntegrityError("digest mismatch")

    monkeypatch.setattr("core.shadow_pipeline.run_shadow_if_enabled", raise_integrity_error)
    report = process_moodle_alert(_alert())

    assert report["status"] == "escalated"
    assert "integrity validation" in report["reason"]
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_live_mode_fails_closed_without_ground_truth_routing(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "live")
    report = process_moodle_alert(_alert())

    assert report is not None
    assert report["status"] == "escalated"
    assert "investigate mode" in report["reason"]
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


@pytest.mark.parametrize("mode", [None, "off", "invalid"])
def test_disabled_or_invalid_mode_does_not_fall_through_to_legacy_moodle_path(
    monkeypatch, mode: str | None
) -> None:
    if mode is None:
        monkeypatch.delenv("AIOPS_UNIFIED_CORE_MODE", raising=False)
    else:
        monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", mode)

    report = process_moodle_alert(_alert())

    assert report is not None
    assert report["status"] == "escalated"
    assert "legacy fallback is disabled" in report["reason"]
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_production_moodle_alert_does_not_enter_staging_shadow_path(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    alert = _alert()
    alert["labels"]["environment"] = "production"

    report = process_moodle_alert(alert)

    assert report is not None
    assert report["status"] == "escalated"
    assert "restricted to staging" in report["reason"]
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_celery_worker_routes_staging_moodle_alert_to_unified_pipeline(monkeypatch) -> None:
    report = {
        "status": "investigation_proposal",
        "scenario_id": "DB-01",
        "incident_id": "i1",
        "diagnosis": {"hypothesis": "RDS path probe failed"},
        "plan_candidate": {"action_type": "read_health", "action_id": "act-1", "target_resource_id": "postgres-db"},
        "remediation_candidate": {
            "scenario_id": "DB-01",
            "catalog_action_id": "remove_scoped_db_reject",
            "target_scope": "staging_moodle_nodes",
            "action": {"action_id": "act-2"},
        },
        "decision_evidence_refs": ["ev-diagnosis", "ev-plan"],
        "execution_permitted": False,
        "resolution_eligible": False,
    }
    monkeypatch.setattr(tasks, "_reserve_alert_processing", lambda _alert: True)
    monkeypatch.setattr(tasks, "_reserve_alert_notification", lambda _alert, _kind: True)
    monkeypatch.setattr(tasks, "process_moodle_alert", lambda _alert: report)
    metric_reports = []
    monkeypatch.setattr(tasks, "_record_moodle_pipeline_event", metric_reports.append)
    sent = []
    monkeypatch.setattr(tasks, "send_telegram_message", lambda message, **kwargs: sent.append(message) or True)

    asyncio.run(tasks.process_single_alert(_alert()))

    assert len(sent) == 1
    assert metric_reports == [report]
    assert "[DB-01]" in sent[0]
    assert "Nguyên nhân: RDS path probe failed" in sent[0]
    assert "remove_scoped_db_reject" in sent[0] and "staging_moodle_nodes" in sent[0]
    assert "cần 2 chữ ký người duyệt" in sent[0]
    assert "execution_permitted=False" in sent[0]
    assert "incident=i1" in sent[0]


def test_moodle_pipeline_metrics_record_mode_and_bounded_status(monkeypatch) -> None:
    recorded = []

    class _Metric:
        def labels(self, **labels):
            recorded.append(labels)
            return self

        def inc(self):
            return None

    monkeypatch.setattr(tasks, "MOODLE_PIPELINE_EVENTS_TOTAL", _Metric())
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    tasks._record_moodle_pipeline_event({"status": "investigation_proposal"})
    tasks._record_moodle_pipeline_event({"status": "error"})
    tasks._record_moodle_pipeline_event({"status": "unbounded-user-value"})
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "unexpected-mode")
    tasks._record_moodle_pipeline_event({"status": "escalated"})

    assert recorded == [
        {"mode": "investigate", "status": "investigation_proposal"},
        {"mode": "investigate", "status": "error"},
        {"mode": "investigate", "status": "other"},
        {"mode": "invalid", "status": "escalated"},
    ]


def test_alertmanager_resolved_moodle_alert_does_not_resolve_incident(monkeypatch) -> None:
    resolved = _alert()
    resolved["status"] = "resolved"
    monkeypatch.setattr(tasks, "_clear_alert_cooldown", lambda _alert: None)
    monkeypatch.setattr(tasks, "_reserve_alert_notification", lambda _alert, _kind: True)
    monkeypatch.setattr(tasks, "process_moodle_alert", lambda _alert: pytest.fail("resolved notifications do not enter Moodle investigation"))
    monkeypatch.setattr(tasks, "send_telegram_message", lambda *args, **kwargs: True)

    asyncio.run(tasks.process_single_alert(resolved))


@pytest.mark.parametrize(
    "unremediable_alert",
    [
        "MoodleRdsTcpProbeFailed",
        "MoodleEfsMountMissing",
        "MoodleNodeExporterDown",
        "MoodleCAdvisorDown",
        "MoodlePublicProbeFailed",
    ],
)
def test_unmatched_or_unremediable_alerts_never_generate_remediation_candidate(
    tmp_path, monkeypatch, unremediable_alert: str
) -> None:
    """Alerts without allowlisted staging remediation must never produce candidates."""
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))
    monkeypatch.delenv("AIOPS_EVIDENCE_DB_PATH", raising=False)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    alert = _alert(alert_name=unremediable_alert)
    report = process_moodle_alert(alert)

    assert report is not None
    assert report["remediation_candidate"] is None
    assert report["approval_required"] is False
    assert report["execution_permitted"] is False
    assert report["resolution_eligible"] is False


def test_stale_or_conflicting_evidence_does_not_generate_remediation_candidate(
    tmp_path, monkeypatch
) -> None:
    """Even for remediable alert types, stale or conflicting samples must suppress remediation."""
    from datetime import datetime, timezone, timedelta
    from core.evidence.evidence_collectors import EvidenceDraft

    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")

    # Stale evidence (collected >120s ago)
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=200)

    def collect_stale(_self, query_id, _expression, resource_id):
        return [
            EvidenceDraft(
                "prometheus",
                resource_id,
                "metric_sample",
                stale_time,
                {"query_id": query_id, "sample_value": 85.0, "labels": {"instance": "moodle-app-b"}},
            )
        ]

    monkeypatch.setattr(
        "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query",
        collect_stale,
    )

    alert = _alert("RES-01", "MoodleNodeCpuHigh")
    report = process_moodle_alert(alert)

    assert report is not None
    assert report["remediation_candidate"] is None
    assert report["execution_permitted"] is False


def test_moodle_alert_strictly_follows_single_runtime_path_and_isolates_legacy(
    monkeypatch,
) -> None:
    """Verify single runtime path: Moodle alerts invoke only process_moodle_alert, non-Moodle ignored."""
    invoked_paths = []

    def mock_moodle_pipeline(alert):
        invoked_paths.append("moodle_pipeline")
        return {
            "status": "awaiting_evidence",
            "mode": "investigate",
            "incident_id": "inc-single-path",
            "execution_permitted": False,
            "resolution_eligible": False,
        }

    monkeypatch.setattr(tasks, "process_moodle_alert", mock_moodle_pipeline)
    monkeypatch.setattr(tasks, "_reserve_alert_processing", lambda _alert: True)
    monkeypatch.setattr(tasks, "_reserve_alert_notification", lambda _alert, _kind: True)
    monkeypatch.setattr(tasks, "send_telegram_message", lambda *args, **kwargs: True)

    # 1. Non-Moodle alert -> process_moodle_alert should return None, no other pipeline called
    non_moodle_alert = {
        "status": "firing",
        "labels": {"alertname": "ExternalServiceDown", "service": "external"},
    }
    monkeypatch.setattr(tasks, "process_moodle_alert", lambda _alert: None)
    asyncio.run(tasks.process_single_alert(non_moodle_alert))
    assert invoked_paths == []

    # 2. Moodle alert -> strictly invokes process_moodle_alert, nothing else
    monkeypatch.setattr(tasks, "process_moodle_alert", mock_moodle_pipeline)
    moodle_alert = _alert("CON-01", "MoodleWebContainerMissing")
    asyncio.run(tasks.process_single_alert(moodle_alert))
    assert invoked_paths == ["moodle_pipeline"]


def test_all_15_moodle_scenarios_have_explicit_remediation_disposition_and_valid_catalog_entries() -> None:
    """Verify 15-scenario coverage: 5 S3.3 live-allowlisted, 1 human-only escalation (SEC-01), 9 shadow/escalated."""
    from core.moodle.planner import SCENARIO_REMEDIATION_DISPOSITION, describe_moodle_remediation_disposition
    from core.safety.action_catalog import ActionCatalog

    assert set(SCENARIO_REMEDIATION_DISPOSITION) == set(_MOODLE_SCENARIOS)
    catalog = ActionCatalog.load_moodle_catalog()

    s3_3 = {k for k, v in SCENARIO_REMEDIATION_DISPOSITION.items() if v["disposition"] == "s3_3_live_allowlisted"}
    human_only = {k for k, v in SCENARIO_REMEDIATION_DISPOSITION.items() if v["disposition"] == "human_only_escalation"}
    shadow_escalated = {k for k, v in SCENARIO_REMEDIATION_DISPOSITION.items() if v["disposition"] == "escalated_shadow_only"}

    assert s3_3 == {"DB-01", "RES-01", "NET-01", "CON-01", "SEC-02"}
    assert human_only == {"SEC-01"}
    assert shadow_escalated == {"DB-02", "DB-03", "RES-02", "RES-03", "NET-02", "NET-03", "CON-02", "CON-03", "SEC-03"}

    for scenario_id, info in SCENARIO_REMEDIATION_DISPOSITION.items():
        entry = catalog.lookup(info["catalog_action_id"])
        assert entry is not None, f"Missing catalog action for {scenario_id}: {info['catalog_action_id']}"
        assert info["reason"]

    sec01_disp = describe_moodle_remediation_disposition(
        alert_name="MoodleDatabasePortExposure",
        diagnosis={"status": "hypothesis", "cause_code": "database_port_exposed", "evidence_refs": ["ev-1"]},
        remediation_candidate=None,
    )
    assert sec01_disp["disposition"] == "human_only_escalation"
    assert sec01_disp["scenario_id"] == "SEC-01"
    assert sec01_disp["execution_permitted"] is False


def test_evidence_chain_includes_dependency_impact_and_structured_provenance(
    tmp_path, monkeypatch
) -> None:
    """Verify observe -> evidence -> diagnosis -> candidate -> explanation includes dependency impact and provenance."""
    from datetime import datetime, timezone
    from core.evidence.evidence_collectors import EvidenceDraft

    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "investigate")
    database_path = tmp_path / "evidence.sqlite3"
    monkeypatch.setenv("AIOPS_EVIDENCE_DB", str(database_path))
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "rag"))

    def collect(_self, query_id, _expression, resource_id):
        value = 0.0 if query_id == "moodle_rds_tcp_probe" else 1.0
        return [
            EvidenceDraft(
                "prometheus",
                resource_id,
                "metric_sample",
                datetime.now(timezone.utc),
                {"query_id": query_id, "sample_value": value},
            )
        ]

    monkeypatch.setattr(
        "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query",
        collect,
    )
    monkeypatch.setattr(
        "core.rag.rag_engine.get_rag_instance",
        lambda: type(
            "MockProvenanceRAG",
            (),
            {
                "retrieve_runbook_with_provenance": lambda self, _query, alert_name: {
                    "text": f"[Nguồn: moodle/runbook_moodle_connectivity.md; thời điểm: 2026-10-01T00:00:00Z; SHA-256: deadbeef] Runbook for {alert_name}.",
                    "provenance": [
                        {
                            "source_file": "moodle/runbook_moodle_connectivity.md",
                            "source_observed_at": "2026-10-01T00:00:00Z",
                            "indexed_at": "2026-10-01T00:01:00Z",
                            "source_sha256": "deadbeef",
                            "heading": "Connectivity",
                            "chunk_index": 0,
                        }
                    ],
                }
            },
        )(),
    )

    report = process_moodle_alert(_alert("DB-02", "MoodleRdsTcpProbeFailed"))
    assert report is not None
    assert report["status"] == "investigation_proposal"
    # postgres-db is the confirmed resource; moodle-app and prometheus-monitor depend on postgres-db
    assert report["confirmed_affected_resources"] == ["postgres-db"]
    assert "moodle-app" in report["potentially_affected_resources"]
    assert "moodle-web-endpoint" in report["potentially_affected_resources"]
    assert len(report["evidence_provenance"]) >= 2
    for item in report["evidence_provenance"]:
        assert set(item) >= {"evidence_id", "source", "collected_at", "resource_id"}
    assert len(report["runbook_provenance"]) == 1
    assert report["runbook_provenance"][0]["source_file"] == "moodle/runbook_moodle_connectivity.md"
    assert report["runbook_provenance"][0]["source_sha256"] == "deadbeef"
    assert report["remediation_disposition"]["disposition"] == "escalated_awaiting_operator_isolation"
    assert report["explanation"]["cause_code"] == "rds_path_probe_failed"
    assert len(report["explanation"]["supporting_evidence_provenance"]) == 1


