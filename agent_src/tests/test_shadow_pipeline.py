from __future__ import annotations

from core.evidence_store import SQLiteEvidenceStore
from core.shadow_pipeline import get_unified_core_mode, run_shadow_if_enabled


def test_unified_core_mode_defaults_off_and_invalid_fails_closed(monkeypatch, caplog) -> None:
    monkeypatch.delenv("AIOPS_UNIFIED_CORE_MODE", raising=False)
    assert get_unified_core_mode() == "off"
    assert get_unified_core_mode("unexpected") == "off"
    assert "failing closed to off" in caplog.text


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
    result = run_shadow_if_enabled(
        {
            "fingerprint": "fixture-fingerprint",
            "status": "firing",
            "labels": {
                "alertname": "PostgreSQLDown",
                "service": "postgres-db",
                "scenario_id": "WRONG-ORACLE-VALUE",
            },
            "annotations": {"summary": "PostgreSQL exporter is down"},
        }
    )
    assert result is not None
    assert result["status"] == "observed"
    assert result["stage"] == "observe"
    assert result["simulated"] is False
    assert result["reason"] == "awaiting_independent_collectors"
    assert database_path.exists()
    with SQLiteEvidenceStore(database_path) as store:
        evidence = store.list_for_incident(result["incident_id"])
    assert len(evidence) == 1
    assert evidence[0].source == "alertmanager"
    assert "scenario_id" not in evidence[0].metadata


def test_shadow_mode_skips_unmapped_live_alert(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", "shadow")
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(tmp_path / "shadow.db"))
    assert run_shadow_if_enabled({"labels": {"alertname": "Unknown"}}) == {
        "status": "skipped",
        "reason": "unknown_resource",
    }
