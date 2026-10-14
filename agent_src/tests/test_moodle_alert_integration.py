from __future__ import annotations

import asyncio
import json

import pytest

from core import tasks
from core.moodle_alert_integration import process_moodle_alert
from core.evidence_store import EvidenceIntegrityError
from core.moodle_contract import load_moodle_ground_truth

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
    from core.evidence_store import SQLiteEvidenceStore

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

    from core.evidence_collectors import EvidenceDraft
    from core.evidence_store import SQLiteEvidenceStore

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
        "core.evidence_collectors.PrometheusEvidenceCollector._collect_query",
        collect,
    )
    monkeypatch.setattr(
        "core.rag_engine.get_rag_instance",
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
    assert len(collected_queries) == 4
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
    assert "hypothesis=RDS path probe failed" in sent[0]
    assert "read_only_proposal=read_health:act-1 target=postgres-db" in sent[0]
    assert "decision_evidence=ev-diagnosis,ev-plan" in sent[0]
    assert "execution_permitted=False" in sent[0]
    assert "resolution_eligible=False" in sent[0]


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
    monkeypatch.setattr(tasks, "_mark_matching_incident_resolved", lambda _alert: pytest.fail("Alertmanager must not resolve Moodle incidents"))
    monkeypatch.setattr(tasks, "_reserve_alert_notification", lambda _alert, _kind: True)
    monkeypatch.setattr(tasks, "send_telegram_message", lambda *args, **kwargs: True)

    asyncio.run(tasks.process_single_alert(resolved))
