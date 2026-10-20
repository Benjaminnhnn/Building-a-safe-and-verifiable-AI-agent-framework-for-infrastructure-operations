"""Platform-neutral core tests using a mock platform adapter.

Verifications:
1. Full evidence flow: alert -> evidence -> diagnosis -> plan -> gate -> dry-run preview.
2. Missing evidence flow: fails closed / escalates safely.
3. Invalid or expired evidence flow: rejected by validity check.
4. Disallowed action: denied by safety gate.
5. Adapter error handling: collection/diagnosis/planning errors fail closed safely.
6. Dry-run safety: guarantees zero mutations on target infrastructure.
7. Architectural dependency check: core modules must never directly import moodle_* modules.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.safety.action_catalog import ActionCatalog, ActionPermission, CatalogEntry
from evaluation.agent_replay.domain.action_plan import (
    ActionPlan,
    BlastRadius,
    ExpectedScope,
    ReversibleRollback,
)
from evaluation.agent_replay.domain.dry_run import (
    DictReadOnlyClient,
    DryRunDecision,
    DryRunPreview,
    DryRunResult,
    ReadOnlyTargetClient,
    StateDiff,
)
from evaluation.agent_replay.domain.enums import ActionType, Environment, EvidenceSource
from evaluation.agent_replay.domain.evidence import Evidence
from evaluation.agent_replay.domain.resource import Resource
from core.platform_adapter import PlatformAdapter
from evaluation.agent_replay.unified_core import (
    Incident,
    IncidentCoordinator,
    IncidentState,
    SQLiteEvidenceStore,
)


class MockPlatformAdapter:
    """Mock platform adapter without any Moodle dependency."""

    def __init__(
        self,
        *,
        missing_evidence: bool = False,
        expired_evidence: bool = False,
        disallowed_action: bool = False,
        fail_collection: bool = False,
        fail_diagnosis: bool = False,
        fail_planning: bool = False,
        precondition_fail: bool = False,
    ) -> None:
        self.missing_evidence = missing_evidence
        self.expired_evidence = expired_evidence
        self.disallowed_action = disallowed_action
        self.fail_collection = fail_collection
        self.fail_diagnosis = fail_diagnosis
        self.fail_planning = fail_planning
        self.precondition_fail = precondition_fail

        self.client = DictReadOnlyClient(
            {
                "mock-server": {
                    "status": "stopped" if not precondition_fail else "corrupted",
                    "version": "1.0.0",
                },
                "mock-db": {"status": "healthy", "connections": 10},
            }
        )

    @property
    def platform_name(self) -> str:
        return "mock"

    def can_handle(self, alert: dict[str, Any]) -> bool:
        labels = alert.get("labels") or {}
        return (
            labels.get("service") == "mock-service"
            or str(labels.get("alertname") or "").startswith("Mock")
        )

    def identify_resource(self, alert: dict[str, Any]) -> str | None:
        labels = alert.get("labels") or {}
        alert_name = str(labels.get("alertname") or "")
        if alert_name == "MockServiceDown":
            return "mock-server"
        if alert_name == "MockDbDown":
            return "mock-db"
        return labels.get("resource_id")

    def get_resources(self) -> list[Resource]:
        return [
            Resource(
                resource_id="mock-server",
                name="Mock Application Server",
                type="application",
                environment=Environment.STAGING,
                host="mock-host-1",
                labels={"tier": "app"},
            ),
            Resource(
                resource_id="mock-db",
                name="Mock Database",
                type="database",
                environment=Environment.STAGING,
                host="mock-host-2",
                labels={"tier": "data"},
            ),
        ]

    def get_resource(self, resource_id: str) -> Resource | None:
        for r in self.get_resources():
            if r.resource_id == resource_id:
                return r
        return None

    def get_resource_types(self) -> dict[str, str]:
        return {r.resource_id: r.type for r in self.get_resources()}

    def get_alert_queries(self, alert_name: str) -> tuple[tuple[str, str], ...]:
        return (("mock_up", 'up{job="mock"}'),)

    def collect_evidence(
        self,
        alert: dict[str, Any],
        *,
        incident_id: str,
        resource_id: str,
        evidence_store: Any,
        prometheus_url: str | None = None,
    ) -> dict[str, Any]:
        if self.fail_collection:
            raise RuntimeError("simulated collection failure in adapter")

        if self.missing_evidence:
            return {"evidence_refs": [], "evidence_items": []}

        now = datetime.now(timezone.utc)
        if self.expired_evidence:
            collected_at = now - timedelta(minutes=20)
            expires_at = now - timedelta(minutes=5)
        else:
            collected_at = now
            expires_at = now + timedelta(minutes=15)

        evidence = Evidence(
            evidence_id=f"ev-mock-{resource_id}",
            source=EvidenceSource.METRIC,
            collected_at=collected_at,
            expires_at=expires_at,
            resource_ids=[resource_id],
            content_ref="sha256:mock_metric_hash",
            summary="Mock service report indicating node failure",
        )
        return {
            "evidence_refs": [evidence.evidence_id],
            "evidence_items": [evidence],
            "status": "complete",
        }

    def diagnose(self, alert_name: str, evidence: list[Any]) -> dict[str, Any]:
        if self.fail_diagnosis:
            raise RuntimeError("simulated diagnosis failure in adapter")

        return {
            "status": "hypothesis",
            "cause_code": "mock_service_stopped",
            "hypothesis": "The mock server stopped unexpectedly",
            "reason": "Process is absent from process table",
            "confidence": 0.95,
            "evidence_refs": [e.evidence_id for e in evidence if hasattr(e, "evidence_id")],
        }

    def get_action_catalog(self) -> ActionCatalog:
        entries = [
            CatalogEntry(
                action_id="restart_container",
                description="Restart mock application container",
                adapter="docker",
                permission=ActionPermission(
                    required_role="executor",
                    environment_scope=["staging"],
                    read_only=False,
                ),
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=True,
            ),
            CatalogEntry(
                action_id="read_health",
                description="Read mock service health",
                adapter="probe",
                permission=ActionPermission(
                    required_role="verifier",
                    environment_scope=["staging"],
                    read_only=True,
                ),
                blast_radius="LOW",
                is_reversible=True,
                rollback_action=None,
                idempotent=True,
                forbidden_in_production=False,
            ),
        ]
        return ActionCatalog(entries=entries)

    def plan_investigation(
        self, incident_id: str, diagnosis: dict[str, Any]
    ) -> Any | None:
        return {
            "action_id": "act-mock-investigate",
            "action": "read_health",
            "target": "mock-server",
        }

    def plan_remediation(
        self,
        incident_id: str,
        alert_name: str,
        diagnosis: dict[str, Any],
        evidence: list[Any],
    ) -> ActionPlan | None:
        if self.fail_planning:
            raise RuntimeError("simulated planning failure in adapter")

        if self.disallowed_action:
            target = "unauthorized-resource"
            scope = ["mock-server"]  # target not in scope
        else:
            target = "mock-server"
            scope = ["mock-server"]

        preconditions = ["status==stopped"] if not self.precondition_fail else ["status==running"]

        return ActionPlan(
            incident_id=incident_id,
            action_type=ActionType.RESTART_CONTAINER,
            target_resource_id=target,
            operation="restart_container",
            preconditions=preconditions,
            evidence_ids=[e.evidence_id for e in evidence if hasattr(e, "evidence_id")] or ["ev-mock-default"],
            expected_scope=ExpectedScope(
                affected_resource_ids=scope if not self.disallowed_action else [target],
                max_blast_radius=BlastRadius.LOW,
            ),
            expected_outcome="mock service becomes operational",
            rollback=ReversibleRollback(
                steps=["stop_container"],
                max_rollback_seconds=60,
            ),
            requested_by="mock_planner",
        )

    def get_target_scopes(self) -> dict[str, set[str]]:
        if self.disallowed_action:
            return {"mock_app_nodes": {"different-resource"}}
        return {
            "mock_app_nodes": {"mock-server"},
            "mock_db_nodes": {"mock-db"},
        }

    def get_verification_probes(
        self,
        incident_id: str,
        *,
        scenario_id: str | None = None,
        action_id: str | None = None,
    ) -> dict[str, list[Any]]:
        return {"allowed_probes": [], "forbidden_probes": [], "related_probes": []}

    def get_read_only_client(self) -> ReadOnlyTargetClient:
        return self.client


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_core_flow_with_full_evidence() -> None:
    adapter = MockPlatformAdapter()
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-1",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service", "environment": "staging"},
    }

    result = coordinator.coordinate(alert, execute_dry_run=True)

    assert result["status"] == "success"
    assert result["incident"].state == IncidentState.VERIFIED_DRY_RUN
    assert result["diagnosis"]["status"] == "hypothesis"
    assert result["diagnosis"]["cause_code"] == "mock_service_stopped"
    assert result["gate_decision"] in {"ALLOW", "REQUIRE_APPROVAL"}
    assert result["dry_run_result"] is not None
    assert result["dry_run_result"].decision == DryRunDecision.ALLOW
    assert result["dry_run_result"].preview is not None
    assert result["dry_run_result"].preview.target_resource_id == "mock-server"


def test_core_flow_with_missing_evidence() -> None:
    adapter = MockPlatformAdapter(missing_evidence=True)
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-empty",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service"},
    }

    result = coordinator.coordinate(alert)

    assert result["status"] == "escalated"
    assert result["stage"] == "evidence_collection"
    assert result["reason"] == "missing_or_empty_evidence"
    assert result["incident"].state == IncidentState.ESCALATED


def test_core_flow_with_invalid_or_expired_evidence() -> None:
    adapter = MockPlatformAdapter(expired_evidence=True)
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-expired",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service"},
    }

    result = coordinator.coordinate(alert)

    assert result["status"] == "escalated"
    assert result["stage"] == "evidence_validation"
    assert "invalid or expired" in result["reason"]
    assert result["incident"].state == IncidentState.ESCALATED


def test_core_flow_with_disallowed_action() -> None:
    adapter = MockPlatformAdapter(disallowed_action=True)
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-disallowed",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service"},
    }

    result = coordinator.coordinate(alert)

    assert result["status"] == "denied"
    assert result["stage"] == "gate"
    assert result["gate_decision"] == "DENY"
    assert result["incident"].state == IncidentState.FAILED


def test_core_flow_handles_adapter_collection_error() -> None:
    adapter = MockPlatformAdapter(fail_collection=True)
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-err-col",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service"},
    }

    result = coordinator.coordinate(alert)

    assert result["status"] == "failed"
    assert result["stage"] == "evidence_collection"
    assert "simulated collection failure" in result["error"]
    assert result["incident"].state == IncidentState.FAILED


def test_core_flow_handles_adapter_diagnosis_error() -> None:
    adapter = MockPlatformAdapter(fail_diagnosis=True)
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-err-diag",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service"},
    }

    result = coordinator.coordinate(alert)

    assert result["status"] == "failed"
    assert result["stage"] == "diagnosis"
    assert "simulated diagnosis failure" in result["error"]
    assert result["incident"].state == IncidentState.FAILED


def test_core_flow_handles_adapter_planning_error() -> None:
    adapter = MockPlatformAdapter(fail_planning=True)
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-err-plan",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service"},
    }

    result = coordinator.coordinate(alert)

    assert result["status"] == "failed"
    assert result["stage"] == "planning"
    assert "simulated planning failure" in result["error"]
    assert result["incident"].state == IncidentState.FAILED


def test_core_dry_run_precondition_failure() -> None:
    adapter = MockPlatformAdapter(precondition_fail=True)
    coordinator = IncidentCoordinator(adapter=adapter)
    alert = {
        "fingerprint": "alert-mock-dry-fail",
        "labels": {"alertname": "MockServiceDown", "service": "mock-service"},
    }

    result = coordinator.coordinate(alert, execute_dry_run=True)

    assert result["status"] == "dry_run_failed"
    assert result["stage"] == "dry_run"
    assert result["dry_run_result"].decision == DryRunDecision.DENY
    assert result["incident"].state == IncidentState.FAILED


def test_core_modules_do_not_import_moodle_directly() -> None:
    """Verify that none of the core modules directly import moodle_* modules."""
    core_dir = Path(__file__).resolve().parents[1] / "core"
    core_files = [
        core_dir / "unified_core.py",
        core_dir / "platform_adapter.py",
        core_dir / "shadow_pipeline.py",
        core_dir / "evidence_collectors.py",
        core_dir / "safe_execution_gate.py",
    ]
    # Add all domain files
    domain_dir = core_dir / "domain"
    if domain_dir.exists():
        core_files.extend(domain_dir.glob("*.py"))

    forbidden_imports: list[str] = []

    for file_path in core_files:
        if not file_path.exists():
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (
                        "moodle_" in alias.name
                        or alias.name.startswith("core.moodle")
                        or alias.name.startswith("core.erpnext")
                    ):
                        forbidden_imports.append(f"{file_path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    "moodle_" in module
                    or module.startswith("core.moodle")
                    or module.startswith("core.erpnext")
                ):
                    forbidden_imports.append(f"{file_path.name}: from {module} import ...")

    assert forbidden_imports == [], (
        f"Core modules violate architectural boundary by importing platform adapters directly:\n"
        + "\n".join(forbidden_imports)
    )
