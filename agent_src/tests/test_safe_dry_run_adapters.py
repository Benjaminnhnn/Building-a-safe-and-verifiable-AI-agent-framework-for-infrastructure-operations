"""S3.3.2 tests: safe adapters remain typed, bounded and side-effect free."""

from __future__ import annotations

import pytest
from core.adapters import (
    SafeActionRequest,
    SafeAnsibleDryRunAdapter,
    SafeDockerDryRunAdapter,
    SafeProbeDryRunAdapter,
)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment
from pydantic import ValidationError


def _action(*, environment: Environment = Environment.STAGING) -> TypedAction:
    return TypedAction(
        action_id="act-db-01-001",
        incident_id="inc-db-01",
        action_type=ActionType.REMOVE_SCOPED_PORT_BLOCK,
        target_resource_id="postgres-db",
        environment=environment,
        reason="three evidence records confirm the scoped staging fault",
        evidence_refs=["ev-1", "ev-2", "ev-3"],
        expected_outcome="synthetic transaction can connect to PostgreSQL",
        reversible=True,
        rollback_plan=RollbackPlan(available=True, method="restore scoped fault"),
    )


def _request(**updates: object) -> SafeActionRequest:
    values: dict[str, object] = {
        "request_id": "req-1",
        "idempotency_key": "idem-1",
        "catalog_action_id": "remove_scoped_db_reject",
        "scenario_id": "DB-01",
        "target_scope": "staging_moodle_nodes",
        "action": _action(),
    }
    values.update(updates)
    return SafeActionRequest(**values)


def test_docker_safe_adapter_accepts_exact_dry_run_boundary() -> None:
    adapter = SafeDockerDryRunAdapter()
    first = adapter.execute(_request())
    duplicate = adapter.execute(_request())
    assert first.success is True
    assert first.executed is False
    assert "catalog_action=remove_scoped_db_reject" in first.sanitized_output
    assert duplicate.duplicate is True


@pytest.mark.parametrize(
    ("adapter", "safe_request"),
    [
        (SafeDockerDryRunAdapter(), _request(catalog_action_id="unrestricted_shell")),
        (SafeDockerDryRunAdapter(), _request(target_scope="moodle-app-b")),
        (SafeDockerDryRunAdapter(), _request(action=_action(environment=Environment.PRODUCTION))),
        (SafeAnsibleDryRunAdapter(), _request()),
    ],
)
def test_safe_adapters_reject_unknown_wrong_scope_production_or_wrong_adapter(
    adapter: SafeDockerDryRunAdapter | SafeAnsibleDryRunAdapter, safe_request: SafeActionRequest
) -> None:
    result = adapter.execute(safe_request)
    assert result.success is False
    assert result.executed is False
    assert result.error_code == "outside_execution_boundary"


def test_probe_adapter_only_accepts_reviewed_probe() -> None:
    action = _action()
    request = _request(
        catalog_action_id="verify_tls_database_connection",
        action=action,
    )
    result = SafeProbeDryRunAdapter().execute(request)
    assert result.success is True
    assert result.executed is False


def test_safe_request_cannot_disable_dry_run() -> None:
    with pytest.raises(ValidationError):
        _request(dry_run=False)
