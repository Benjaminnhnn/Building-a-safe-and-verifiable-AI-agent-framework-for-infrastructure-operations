from unittest.mock import Mock

import pytest

from core.rag_engine import RAGEngine, RUNBOOK_ALERT_NAMES, _chunk_markdown


def test_chunk_markdown_splits_by_heading_and_size() -> None:
    content = "# First\n\n" + ("a" * 80) + "\n\n## Second\n\n" + ("b" * 80)

    chunks = _chunk_markdown(content, max_chars=60)

    assert len(chunks) >= 4
    assert chunks[0][0] == "First"
    assert any(heading == "Second" for heading, _ in chunks)
    assert all(len(document) <= 60 for _, document in chunks)


def test_save_admin_solution_writes_to_incident_memory() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.incident_memory = Mock()

    engine.save_admin_solution(
        incident_id="abc12345",
        alert_name="WebEndpointDown",
        incident_details="frontend is down",
        admin_feedback="check logs first",
        reviewed_solution="check logs, then restart",
        review_status="revised",
    )

    kwargs = engine.incident_memory.upsert.call_args.kwargs
    assert kwargs["metadatas"][0]["source"] == "admin_feedback"
    assert kwargs["metadatas"][0]["document_type"] == "admin_feedback"
    assert kwargs["metadatas"][0]["source_observed_at"]
    assert kwargs["metadatas"][0]["indexed_at"]
    assert len(kwargs["metadatas"][0]["source_sha256"]) == 64
    assert "check logs, then restart" in kwargs["documents"][0]


def test_save_admin_solution_redacts_secrets_at_storage_boundary() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.incident_memory = Mock()

    engine.save_admin_solution(
        incident_id="abc12345",
        alert_name="WebEndpointDown",
        incident_details="Alert context db_password=context-secret",
        admin_feedback="Use token=feedback-secret",
        reviewed_solution="Restart with api_key=solution-secret",
        review_status="accepted",
    )

    stored_document = engine.incident_memory.upsert.call_args.kwargs["documents"][0]
    for secret in ("context-secret", "feedback-secret", "solution-secret"):
        assert secret not in stored_document
    assert stored_document.count("[REDACTED]") == 3


def test_query_knowledge_keeps_standard_and_dynamic_sources_separate() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.standard_runbooks = Mock()
    engine.standard_runbooks.count.return_value = 1
    engine.standard_runbooks.query.return_value = {
        "documents": [["standard procedure"]],
        "metadatas": [[{"source_file": "web_endpoint_down.md"}]],
    }
    engine.incident_memory = Mock()
    engine.incident_memory.count.return_value = 1
    engine.incident_memory.query.return_value = {
        "documents": [["admin-reviewed solution"]],
        "metadatas": [[{"source": "admin_feedback"}]],
    }

    result = engine.query_knowledge("frontend web endpoint down")

    assert "Quy trình chuẩn từ runbook" in result
    assert "[Nguồn: web_endpoint_down.md;" in result
    assert "Kinh nghiệm từ incident và feedback trước đây" in result
    assert "[Nguồn: admin_feedback;" in result


def test_retrieval_renders_source_time_and_hash_provenance() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.standard_runbooks = Mock()
    engine.standard_runbooks.count.return_value = 1
    engine.standard_runbooks.query.return_value = {
        "documents": [["verified runbook steps"]],
        "metadatas": [[{"source_file": "safe.md", "source_observed_at": "2026-09-24T00:00:00+00:00", "source_sha256": "a" * 64}]],
        "distances": [[0.0]],
    }
    engine.incident_memory = Mock()
    engine.incident_memory.count.return_value = 0

    result = engine.query_knowledge("verified runbook")

    assert "thời điểm: 2026-09-24T00:00:00+00:00" in result
    assert f"SHA-256: {'a' * 64}" in result


def test_query_knowledge_filters_by_alert_name() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.standard_runbooks = Mock()
    engine.standard_runbooks.count.return_value = 1
    engine.standard_runbooks.query.return_value = {
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    engine.incident_memory = Mock()
    engine.incident_memory.count.return_value = 0

    result = engine.query_knowledge("critical cpu usage", alert_name="CriticalCPUUsage")

    assert result == ""
    assert engine.standard_runbooks.query.call_args.kwargs["where"] == {
        "alert_name": "CriticalCPUUsage"
    }


def test_standard_runbook_query_excludes_incident_memory() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.standard_runbooks = Mock()
    engine.standard_runbooks.count.return_value = 1
    engine.standard_runbooks.query.return_value = {
        "documents": [["authored Moodle guidance"]],
        "metadatas": [[{
            "source_file": "moodle/runbook_moodle_connectivity.md",
            "source_observed_at": "2026-10-05T00:00:00+00:00",
            "source_sha256": "a" * 64,
        }]],
        "distances": [[0.0]],
    }
    engine.incident_memory = Mock()

    result = engine.query_standard_runbooks(
        "Moodle synthetic transaction failed",
        alert_name="MoodleSyntheticTransactionFailed",
    )

    assert "authored Moodle guidance" in result
    assert f"SHA-256: {'a' * 64}" in result
    assert engine.incident_memory.query.call_count == 0


def test_retrieve_drops_documents_above_distance_threshold() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    collection = Mock()
    collection.count.return_value = 1
    collection.query.return_value = {
        "documents": [["unrelated nginx runbook"]],
        "metadatas": [[{"source_file": "runbook_nginx.md"}]],
        "distances": [[99.0]],
    }

    result = engine._retrieve(collection, "critical cpu usage", alert_name="CriticalCPUUsage")

    assert result == ""


def test_moodle_runbooks_are_ingested_recursively_with_alert_provenance() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.standard_runbooks = Mock()

    engine._ingest_initial_data()

    indexed = [
        metadata
        for call in engine.standard_runbooks.upsert.call_args_list
        for metadata in call.kwargs["metadatas"]
    ]
    moodle_rows = [row for row in indexed if str(row["source_file"]).startswith("moodle/")]
    assert {row["source_file"] for row in moodle_rows} == {
        "moodle/runbook_moodle_connectivity.md",
        "moodle/runbook_moodle_service.md",
        "moodle/runbook_moodle_storage_resources.md",
        "moodle/runbook_moodle_monitoring.md",
        "moodle/runbook_moodle_security.md",
    }
    assert {row["alert_name"] for row in moodle_rows} >= {
        "MoodleDatabaseEndpointInvalid",
        "MoodleDatabasePortExposure",
        "MoodlePublicProbeFailed",
        "MoodleEfsMountMissing",
        "MoodleNodeExporterDown",
    }
    assert all(len(row["source_sha256"]) == 64 for row in moodle_rows)
    assert all(row["source_observed_at"] and row["indexed_at"] for row in moodle_rows)
    assert all("ground_truth" not in row["source_file"] for row in indexed)


def test_initial_runbook_ingestion_fails_closed_when_old_revision_cannot_be_removed() -> None:
    engine = RAGEngine.__new__(RAGEngine)
    engine.standard_runbooks = Mock()
    engine.standard_runbooks.delete.side_effect = OSError("vector store unavailable")

    with pytest.raises(RuntimeError, match="Unable to replace indexed runbook"):
        engine._ingest_initial_data()

    engine.standard_runbooks.upsert.assert_not_called()


def test_moodle_alert_mapping_has_no_unmapped_configured_alerts() -> None:
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    rule_files = [
        repo / "ansible/config/moodle_alert_rules.yml",
        repo / "ansible/config/alert_rules.yml",
    ]
    configured = {
        line.strip().split(":", 1)[1].strip()
        for path in rule_files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- alert:") and "Moodle" in line
    }
    mapped = {name for source, names in RUNBOOK_ALERT_NAMES.items() if source.startswith("moodle/") for name in names}
    assert configured <= mapped
