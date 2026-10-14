from __future__ import annotations

import core.shadow_pipeline as shadow_pipeline
from core.evidence_store import SQLiteEvidenceStore
from core.shadow_pipeline import _alert_fingerprint, get_unified_core_mode, run_shadow_if_enabled


def test_unified_core_mode_defaults_off_and_invalid_fails_closed(monkeypatch, caplog) -> None:
    monkeypatch.delenv("AIOPS_UNIFIED_CORE_MODE", raising=False)
    assert get_unified_core_mode() == "off"
    assert get_unified_core_mode("unexpected") == "off"
    assert "failing closed to off" in caplog.text


def test_fallback_alert_fingerprint_is_independent_of_label_order() -> None:
    first = {"alertname": "MoodleNodeCpuHigh", "instance": "moodle-app-a", "service": "moodle"}
    reordered = {"service": "moodle", "instance": "moodle-app-a", "alertname": "MoodleNodeCpuHigh"}

    assert _alert_fingerprint({"labels": first}, first) == _alert_fingerprint(
        {"labels": reordered}, reordered
    )


def test_off_mode_does_not_create_shadow_state(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "must-not-exist.db"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "off")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(database_path))
    assert run_shadow_if_enabled({"labels": {"scenario_id": "DB-01"}}) is None
    assert not database_path.exists()


def test_shadow_mode_records_live_alert_without_ground_truth_oracle(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "shadow.db"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(database_path))
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    result = run_shadow_if_enabled(
        {
            "fingerprint": "fixture-fingerprint",
            "status": "firing",
            "labels": {
                "alertname": "PostgreSQLDown",
                "signal": "password=shadow-secret",
                "service": "postgres-db",
                "scenario_id": "WRONG-ORACLE-VALUE",
            },
            "annotations": {
                "summary": "PostgreSQL exporter is down; token=annotation-secret"
            },
        }
    )
    assert result is not None
    assert result["status"] == "observed"
    assert result["stage"] == "observe"
    assert result["simulated"] is False
    assert result["reason"] == "no evidence-derived decision rule is approved for this alert"
    assert result["evidence_status"] == "partial"
    assert result["execution_permitted"] is False
    assert result["resolution_eligible"] is False
    assert len(result["decision_evidence_refs"]) == 1
    assert database_path.exists()
    with SQLiteEvidenceStore(database_path) as store:
        evidence = store.list_for_incident(result["incident_id"])
    assert len(evidence) == 5
    assert {item.source for item in evidence} == {
        "alertmanager", "resource_inventory", "ai_decision"
    }
    assert sum(item.evidence_type.value == "decision" for item in evidence) == 1
    assert not any("scenario_id" in str(item.metadata) for item in evidence)
    serialized = " ".join(item.summary + str(item.metadata) for item in evidence)
    assert "shadow-secret" not in serialized
    assert "annotation-secret" not in serialized


def test_shadow_recollects_completed_alert_without_fresh_diagnosis_evidence(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "shadow-retry.db"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(database_path))
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    alert = {
        "fingerprint": "repeat-fingerprint",
        "startsAt": "2026-10-06T02:00:00Z",
        "status": "firing",
        "labels": {"alertname": "PostgreSQLDown", "service": "postgres-db"},
        "annotations": {"summary": "PostgreSQL exporter is down"},
    }
    collect_calls = 0
    collect = shadow_pipeline.collect_shadow_evidence

    def track_collection(*args, **kwargs):
        nonlocal collect_calls
        collect_calls += 1
        return collect(*args, **kwargs)

    monkeypatch.setattr(shadow_pipeline, "collect_shadow_evidence", track_collection)

    first = run_shadow_if_enabled(alert)
    second = run_shadow_if_enabled(alert)
    assert collect_calls == 2
    later_episode = run_shadow_if_enabled(
        alert | {"startsAt": "2026-10-06T03:00:00Z"}
    )

    assert second["incident_id"] == first["incident_id"]
    assert collect_calls == 3
    assert later_episode["incident_id"] != first["incident_id"]
    with SQLiteEvidenceStore(database_path) as store:
        assert len(store.list_for_incident(first["incident_id"])) > len(
            first["evidence_refs"]
        ) + len(first["decision_evidence_refs"])
        checkpoint = store.load_latest_checkpoint(first["incident_id"])
    assert checkpoint is not None
    assert checkpoint["stage"] == "shadow_complete"


def test_shadow_alert_claim_in_progress_does_not_collect(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "shadow-in-progress.db"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(database_path))
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.setattr(
        SQLiteEvidenceStore,
        "claim_checkpoint_run",
        lambda self, **_: False,
    )
    result = run_shadow_if_enabled(
        {
            "fingerprint": "already-claimed",
            "startsAt": "2026-10-06T04:00:00Z",
            "status": "firing",
            "labels": {"alertname": "PostgreSQLDown", "service": "postgres-db"},
            "annotations": {"summary": "PostgreSQL exporter is down"},
        }
    )

    assert result["status"] == "in_progress"
    with SQLiteEvidenceStore(database_path) as store:
        assert store.query() == []


def test_shadow_mode_skips_unmapped_live_alert(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(tmp_path / "shadow.db"))
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    assert run_shadow_if_enabled({"labels": {"alertname": "Unknown"}}) == {
        "status": "skipped",
        "reason": "unknown_resource",
    }


def test_shadow_mode_maps_live_moodle_alert_contract(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "shadow.db"
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(database_path))
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    result = run_shadow_if_enabled(
        {
            "fingerprint": "moodle-live-fingerprint",
            "status": "firing",
            "labels": {
                "alertname": "MoodleRdsTcpProbeFailed",
                "service": "moodle-rds",
                "environment": "staging",
            },
            "annotations": {"summary": "Moodle RDS TCP probe failed"},
        }
    )

    assert result is not None
    assert result["status"] == "observed"
    with SQLiteEvidenceStore(database_path) as store:
        evidence = store.list_for_incident(result["incident_id"])
    assert evidence[0].resource_id == "postgres-db"
