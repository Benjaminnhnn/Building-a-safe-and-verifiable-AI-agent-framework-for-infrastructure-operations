from unittest.mock import Mock

from core.rag_engine import RAGEngine, _chunk_markdown


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
