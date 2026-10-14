from __future__ import annotations

import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError
from core.adapters import ActionRequest, FakeDockerAdapter
from core.dependency_graph import DependencyGraph, DependencyGraphError
from core.evidence_store import (
    EvidenceConflictError,
    EvidenceIntegrityError,
    SQLiteEvidenceStore,
    evidence_content_hash,
)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment, EvidenceType, ResourceType
from core.schema.evidence import Evidence
from core.schema.resource import Resource


def _evidence(
    evidence_id: str,
    *,
    source: str = "prometheus",
    resource_id: str = "postgres-db",
    collected_at: datetime | None = None,
    observed_at: datetime | None = None,
    summary: str = "database unavailable",
    evidence_type: EvidenceType = EvidenceType.PROBE,
    redacted: bool = True,
    metadata: dict[str, str] | None = None,
    raw_ref: str | None = "fixture:db-01",
) -> Evidence:
    item = Evidence(
        evidence_id=evidence_id,
        incident_id="inc-db-01",
        resource_id=resource_id,
        evidence_type=evidence_type,
        source=source,
        observed_at=observed_at,
        collected_at=collected_at or datetime.now(timezone.utc),
        summary=summary,
        redacted=redacted,
        raw_ref=raw_ref,
        content_hash="pending",
        metadata=metadata or {"scenario_id": "DB-01"},
    )
    return item.model_copy(update={"content_hash": evidence_content_hash(item)})


def test_evidence_and_metadata_are_immutable_after_hashing() -> None:
    item = _evidence("ev-immutable")
    with pytest.raises(ValidationError):
        item.summary = "changed after hashing"
    with pytest.raises(TypeError, match="immutable"):
        item.metadata["scenario_id"] = "DB-02"
    assert item.content_hash == evidence_content_hash(item)


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
        assert reopened.verify_integrity() == 1


def test_concurrent_identical_evidence_append_is_idempotent(tmp_path) -> None:
    database = tmp_path / "concurrent-evidence.db"
    item = _evidence("ev-concurrent-idempotency")
    with SQLiteEvidenceStore(database):
        pass
    barrier = Barrier(2)

    def append_from_connection() -> bool:
        with SQLiteEvidenceStore(database) as store:
            barrier.wait(timeout=5)
            return store.append(item)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: append_from_connection(), range(2)))

    assert sorted(results) == [False, True]
    with SQLiteEvidenceStore(database) as store:
        assert store.list_for_incident(item.incident_id) == [item]


def test_concurrent_conflicting_evidence_append_rejects_one_record(tmp_path) -> None:
    database = tmp_path / "concurrent-evidence-conflict.db"
    first = _evidence("ev-concurrent-conflict", summary="first observation")
    second = _evidence("ev-concurrent-conflict", summary="second observation")
    with SQLiteEvidenceStore(database):
        pass
    barrier = Barrier(2)

    def append_from_connection(item: Evidence) -> str:
        with SQLiteEvidenceStore(database) as store:
            barrier.wait(timeout=5)
            try:
                return "inserted" if store.append(item) else "duplicate"
            except EvidenceConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append_from_connection, (first, second)))

    assert sorted(results) == ["conflict", "inserted"]
    with SQLiteEvidenceStore(database) as store:
        stored = store.list_for_incident(first.incident_id)
        assert len(stored) == 1
        assert stored[0] in (first, second)
        assert store.verify_integrity() == 1


def test_evidence_store_revalidates_model_copy_before_persisting(tmp_path) -> None:
    corrupted = _evidence("ev-copy-bypass").model_copy(
        update={"summary": "db_password=hunter2", "redacted": False}
    )
    corrupted = corrupted.model_copy(
        update={"content_hash": evidence_content_hash(corrupted)}
    )

    with SQLiteEvidenceStore(tmp_path / "reject-copy-bypass.db") as store:
        with pytest.raises(EvidenceIntegrityError, match="failed validation"):
            store.append(corrupted)
        assert store.query(incident_id="inc-db-01") == []


def test_evidence_preserves_observation_and_collection_timestamps(tmp_path) -> None:
    observed_at = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    collected_at = observed_at + timedelta(seconds=7)
    item = _evidence("ev-time-provenance", observed_at=observed_at, collected_at=collected_at)
    with SQLiteEvidenceStore(tmp_path / "evidence-time.db") as store:
        store.append(item)
        restored = store.get_by_id(item.evidence_id)

    assert restored is not None
    assert restored.observed_at == observed_at
    assert restored.collected_at == collected_at


def test_evidence_rejects_naive_observation_timestamp() -> None:
    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        Evidence(
            evidence_id="ev-naive-observed",
            incident_id="inc-db-01",
            resource_id="postgres-db",
            source="prometheus",
            observed_at=datetime(2026, 10, 5),
            collected_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
            summary="database unavailable",
            content_hash="pending",
        )


@pytest.mark.parametrize(
    "field_value",
    [
        {"summary": "database password=hunter2"},
        {"summary": "db_password=hunter2"},
        {"metadata": {"token": "Bearer secret-value"}},
        {"metadata": {"private_key": "not-pem-private-material"}},
        {"raw_ref": "https://user:password@host/evidence"},
        {"redacted": False},
    ],
)
def test_evidence_rejects_unredacted_or_sensitive_values(field_value) -> None:
    with pytest.raises(ValueError, match="redacted|secret"):
        _evidence("ev-sensitive", **field_value)


@pytest.mark.parametrize(
    "field, value",
    [
        ("evidence_id", "ev-api_key=secret-value"),
        ("incident_id", "inc-password=hunter2"),
        ("resource_id", "resource-token=secret-value"),
    ],
)
def test_evidence_rejects_secrets_in_identity_fields(field: str, value: str) -> None:
    attributes = {
        "evidence_id": "ev-sensitive-identity",
        "incident_id": "inc-db-01",
        "resource_id": "postgres-db",
        "source": "prometheus",
        "summary": "database unavailable",
        "content_hash": "pending",
    }
    attributes[field] = value
    with pytest.raises(ValueError, match="secret"):
        Evidence(**attributes)


def test_evidence_query_preserves_append_order_for_equal_timestamps(tmp_path) -> None:
    path = tmp_path / "same-timestamp.db"
    observed_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    earlier_state = _evidence("ev-z-transition", collected_at=observed_at, summary="VERIFYING")
    terminal_state = _evidence("ev-a-transition", collected_at=observed_at, summary="RESOLVED")
    with SQLiteEvidenceStore(path) as store:
        store.append(earlier_state)
        store.append(terminal_state)
        records = store.query(incident_id="inc-db-01")

    assert [record.evidence_id for record in records] == [
        "ev-z-transition",
        "ev-a-transition",
    ]


@pytest.mark.parametrize("reference_exists_in_other_incident", [False, True])
def test_integrity_scan_rejects_dangling_or_cross_incident_decision_refs(
    tmp_path, reference_exists_in_other_incident
) -> None:
    path = tmp_path / "decision-refs.db"
    with SQLiteEvidenceStore(path) as store:
        if reference_exists_in_other_incident:
            other = _evidence("ev-other").model_copy(update={"incident_id": "inc-other"})
            other = other.model_copy(
                update={"content_hash": evidence_content_hash(other)}
            )
            store.append(other)
        decision = _evidence("ev-decision", source="ai_decision").model_copy(
            update={
                "evidence_type": EvidenceType.DECISION,
                "metadata": {
                    "decision_kind": "diagnosis",
                    "decision_json": json.dumps({"evidence_refs": ["ev-other"]}),
                },
            }
        )
        decision = decision.model_copy(
            update={"content_hash": evidence_content_hash(decision)}
        )
        store.append(decision)

        with pytest.raises(EvidenceIntegrityError, match="missing or misbound"):
            store.verify_integrity()


def test_evidence_read_detects_tampered_canonical_record(tmp_path) -> None:
    path = tmp_path / "evidence-tampered.db"
    item = _evidence("ev-tampered")
    with SQLiteEvidenceStore(path) as store:
        store.append(item)

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER evidence_no_update")
    row = connection.execute(
        "SELECT canonical_json FROM evidence WHERE evidence_id = ?",
        (item.evidence_id,),
    ).fetchone()
    payload = json.loads(row[0])
    payload["summary"] = "changed after collection"
    connection.execute(
        "UPDATE evidence SET canonical_json = ? WHERE evidence_id = ?",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")), item.evidence_id),
    )
    connection.commit()
    connection.close()

    with SQLiteEvidenceStore(path) as store:
        with pytest.raises(EvidenceIntegrityError, match="digest mismatch"):
            store.get_by_id(item.evidence_id)
        with pytest.raises(EvidenceIntegrityError, match="digest mismatch"):
            store.list_for_incident(item.incident_id)


def test_evidence_read_detects_tampered_index_column(tmp_path) -> None:
    path = tmp_path / "evidence-index-tampered.db"
    item = _evidence("ev-index-tampered")
    with SQLiteEvidenceStore(path) as store:
        store.append(item)

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER evidence_no_update")
    connection.execute(
        "UPDATE evidence SET source = ? WHERE evidence_id = ?",
        ("forged-source", item.evidence_id),
    )
    connection.commit()
    connection.close()

    with SQLiteEvidenceStore(path) as store:
        with pytest.raises(EvidenceIntegrityError, match="index mismatch"):
            store.get_by_id(item.evidence_id)
        with pytest.raises(EvidenceIntegrityError, match="index mismatch"):
            store.verify_integrity()


def test_conflicting_duplicate_and_bad_hash_are_rejected(tmp_path) -> None:
    with SQLiteEvidenceStore(tmp_path / "evidence.db") as store:
        assert store.append(_evidence("ev-1")) is True
        with pytest.raises(EvidenceConflictError):
            store.append(_evidence("ev-1", summary="different"))

        bad = _evidence("ev-2")
        bad = bad.model_copy(update={"content_hash": "sha256:wrong"})
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


def test_query_filters_by_evidence_type_with_other_scopes(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    with SQLiteEvidenceStore(tmp_path / "evidence-type.db") as store:
        store.append(_evidence("ev-probe", collected_at=now, evidence_type=EvidenceType.PROBE))
        store.append(_evidence("ev-metric", collected_at=now, evidence_type=EvidenceType.METRIC))
        store.append(
            _evidence(
                "ev-other-resource",
                resource_id="moodle-app",
                collected_at=now,
                evidence_type=EvidenceType.METRIC,
            )
        )

        results = store.query(
            incident_id="inc-db-01",
            resource_id="postgres-db",
            evidence_type=EvidenceType.METRIC,
            collected_from=now - timedelta(seconds=1),
        )

    assert [item.evidence_id for item in results] == ["ev-metric"]


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


def test_checkpoint_run_claim_is_exclusive_recoverable_and_owner_bound(tmp_path) -> None:
    database = tmp_path / "run-claims.db"
    with SQLiteEvidenceStore(database) as first, SQLiteEvidenceStore(database) as second:
        assert first.claim_checkpoint_run(run_id="run-1", owner_token="worker-a")
        assert not second.claim_checkpoint_run(run_id="run-1", owner_token="worker-b")
        assert not second.release_checkpoint_run(run_id="run-1", owner_token="worker-b")

        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        first._connection.execute(
            "UPDATE checkpoint_run_claims SET lease_expires_at = ? WHERE run_id = ?",
            (expired.isoformat(), "run-1"),
        )
        first._connection.commit()
        assert second.claim_checkpoint_run(run_id="run-1", owner_token="worker-b")
        assert not first.release_checkpoint_run(run_id="run-1", owner_token="worker-a")

        second.complete_checkpoint_run(
            run_id="run-1",
            owner_token="worker-b",
            incident_id="incident-1",
            stage="complete",
            sequence_number=1,
            payload={"status": "ok"},
            updated_at=datetime.now(timezone.utc),
        )
        assert second.load_latest_checkpoint("run-1")["payload"] == {"status": "ok"}
        assert second._connection.execute(
            "SELECT 1 FROM checkpoint_run_claims WHERE run_id = ?", ("run-1",)
        ).fetchone() is None


def test_checkpoint_run_claim_allows_only_one_concurrent_worker(tmp_path) -> None:
    database = tmp_path / "run-claim-race.db"
    with SQLiteEvidenceStore(database):
        pass
    barrier = Barrier(2)

    def claim(owner: str) -> bool:
        with SQLiteEvidenceStore(database) as store:
            barrier.wait(timeout=5)
            return store.claim_checkpoint_run(run_id="same-run", owner_token=owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    assert results.count(True) == 1


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


def test_dependency_graph_mermaid_export_is_deterministic_and_escaped() -> None:
    graph = DependencyGraph(
        [
            _resource('moodle "web"', ["postgres"]),
            _resource("postgres", []),
        ]
    )

    diagram = graph.to_mermaid()

    assert diagram == graph.to_mermaid()
    assert 'r1["postgres (application) [postgres]"]' in diagram
    assert 'r0["moodle \\"web\\" (application) [moodle \\"web\\"]"]' in diagram
    assert "r1 --> r0" in diagram


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
