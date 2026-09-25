from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from core.adapters import ActionRequest, FakeDockerAdapter
from core.dependency_graph import DependencyGraph, DependencyGraphError
from core.evidence_store import (
    EvidenceConflictError,
    EvidenceIntegrityError,
    SQLiteEvidenceStore,
    evidence_content_hash,
)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment, ResourceType
from core.schema.evidence import Evidence
from core.schema.resource import Resource


def _evidence(
    evidence_id: str,
    *,
    source: str = "prometheus",
    resource_id: str = "postgres-db",
    collected_at: datetime | None = None,
    summary: str = "database unavailable",
) -> Evidence:
    item = Evidence(
        evidence_id=evidence_id,
        incident_id="inc-db-01",
        resource_id=resource_id,
        source=source,
        collected_at=collected_at or datetime.now(timezone.utc),
        summary=summary,
        raw_ref="fixture:db-01",
        content_hash="pending",
        metadata={"scenario_id": "DB-01"},
    )
    item.content_hash = evidence_content_hash(item)
    return item


def _action(action_type: ActionType = ActionType.START_CONTAINER) -> TypedAction:
    return TypedAction(
        action_id="act-db-01",
        incident_id="inc-db-01",
        action_type=action_type,
        target_resource_id="postgres-db",
        environment=Environment.STAGING,
        reason="evidence based",
        evidence_refs=["ev-1"],
        expected_outcome="healthy",
        reversible=True,
        rollback_plan=RollbackPlan(available=True, method="stop container"),
    )


def _resource(resource_id: str, depends_on: list[str]) -> Resource:
    return Resource(
        resource_id=resource_id,
        name=resource_id,
        type=ResourceType.APPLICATION,
        environment=Environment.STAGING,
        owner_role="infrastructure_engineer",
        depends_on=depends_on,
    )


def test_evidence_persists_and_duplicate_is_idempotent(tmp_path) -> None:
    path = tmp_path / "evidence.db"
    item = _evidence("ev-1")
    with SQLiteEvidenceStore(path) as store:
        assert store.append(item) is True
        assert store.append(item) is False

    with SQLiteEvidenceStore(path) as reopened:
        assert reopened.get_by_id("ev-1") == item
        assert reopened.list_for_incident("inc-db-01") == [item]


def test_conflicting_duplicate_and_bad_hash_are_rejected(tmp_path) -> None:
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        assert store.append(_evidence("ev-1")) is True
        with pytest.raises(EvidenceConflictError):
            store.append(_evidence("ev-1", summary="different"))

        bad = _evidence("ev-2")
        bad.content_hash = "sha256:wrong"
        with pytest.raises(EvidenceIntegrityError):
            store.append(bad)


def test_database_triggers_block_update_and_delete(tmp_path) -> None:
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        store.append(_evidence("ev-1"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._connection.execute("UPDATE evidence SET source='log' WHERE evidence_id='ev-1'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._connection.execute("DELETE FROM evidence WHERE evidence_id='ev-1'")


def test_query_filters_by_source_resource_and_time(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        store.append(_evidence("ev-1", collected_at=now - timedelta(minutes=2)))
        store.append(_evidence("ev-2", source="log", collected_at=now - timedelta(minutes=1)))
        store.append(_evidence("ev-3", resource_id="moodle-app", collected_at=now))

        assert [item.evidence_id for item in store.query(source="log")] == ["ev-2"]
        assert [item.evidence_id for item in store.query(resource_id="moodle-app")] == ["ev-3"]
        assert [item.evidence_id for item in store.query(collected_from=now - timedelta(seconds=90))] == [
            "ev-2",
            "ev-3",
        ]


def test_checkpoint_upsert_and_latest(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        store.save_checkpoint(
            run_id="run-1",
            incident_id="inc-1",
            stage="observe",
            sequence_number=0,
            payload={"count": 1},
            updated_at=now,
        )
        store.save_checkpoint(
            run_id="run-1",
            incident_id="inc-1",
            stage="triage",
            sequence_number=1,
            payload={"count": 2},
            updated_at=now,
        )
        assert store.load_latest_checkpoint("run-1")["stage"] == "triage"
        assert len(store.list_checkpoints("run-1")) == 2


def test_dependency_graph_traversal_is_deterministic() -> None:
    graph = DependencyGraph(
        [
            _resource("postgres", []),
            _resource("moodle", ["postgres"]),
            _resource("endpoint", ["moodle"]),
        ]
    )
    assert graph.upstream("endpoint") == ["moodle", "postgres"]
    assert graph.downstream("postgres") == ["moodle", "endpoint"]
    assert graph.impact("postgres") == ["postgres", "moodle", "endpoint"]


@pytest.mark.parametrize(
    "resources,error",
    [
        ([_resource("a", []), _resource("a", [])], "duplicate"),
        ([_resource("a", ["missing"])], "missing"),
        ([_resource("a", ["b"]), _resource("b", ["a"])], "cycle"),
    ],
)
def test_dependency_graph_rejects_invalid_input(resources: list[Resource], error: str) -> None:
    with pytest.raises(DependencyGraphError, match=error):
        DependencyGraph(resources)


def test_fake_adapter_is_dry_run_sanitized_and_idempotent() -> None:
    adapter = FakeDockerAdapter()
    request = ActionRequest(
        request_id="req-1",
        idempotency_key="idem-1",
        action=_action(),
        dry_run=True,
    )
    first = adapter.execute(request)
    second = adapter.execute(request)
    assert first.success is True
    assert first.executed is False
    assert "fixture-secret" not in first.sanitized_output
    assert second.duplicate is True
    assert adapter.execution_count == 1


@pytest.mark.parametrize(
    ("adapter", "error_code"),
    [
        (FakeDockerAdapter(timeout_actions={ActionType.START_CONTAINER}), "timeout"),
        (FakeDockerAdapter(fail_actions={ActionType.START_CONTAINER}), "simulated_failure"),
    ],
)
def test_fake_adapter_failure_modes(adapter: FakeDockerAdapter, error_code: str) -> None:
    result = adapter.execute(
        ActionRequest(
            request_id="req-1",
            idempotency_key="idem-1",
            action=_action(),
        )
    )
    assert result.success is False
    assert result.error_code == error_code
