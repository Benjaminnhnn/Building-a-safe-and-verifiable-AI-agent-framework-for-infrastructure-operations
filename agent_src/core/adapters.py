"""Infrastructure adapter contracts and deterministic fake implementations."""

from __future__ import annotations

import re
from typing import Literal, Protocol

from core.action_catalog import ActionCatalog
from core.schema.action import TypedAction
from core.schema.common import ActionType, Environment
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class AdapterCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_name: str
    action_types: list[ActionType]
    environments: list[Environment]
    target_resource_ids: list[str] = Field(default_factory=list)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    idempotency_key: str
    action: TypedAction
    dry_run: StrictBool = True
    timeout_seconds: StrictInt = Field(default=30, gt=0, le=300)


class SafeActionRequest(BaseModel):
    """A typed, staging-only request for the safe dry-run adapter boundary.

    There is intentionally no command, host, shell fragment, or AWS argument
    field. Live execution uses SafeLiveActionRequest with the same bounded
    catalog fields and a separate approval path.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    idempotency_key: str
    catalog_action_id: str
    scenario_id: str
    target_scope: str
    action: TypedAction
    dry_run: Literal[True] = True
    timeout_seconds: StrictInt = Field(default=30, gt=0, le=120)


class SafeLiveActionRequest(BaseModel):
    """Typed intent for one separately approved controlled-live action."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    idempotency_key: str
    catalog_action_id: str
    scenario_id: str
    target_scope: str
    action: TypedAction
    dry_run: Literal[False] = False
    timeout_seconds: StrictInt = Field(default=30, gt=0, le=120)


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    idempotency_key: str
    adapter_name: str
    success: StrictBool
    executed: StrictBool
    duplicate: StrictBool = False
    sanitized_output: str
    error_code: str | None = None


class InfrastructureAdapter(Protocol):
    def capabilities(self) -> AdapterCapability: ...

    def execute(self, request: ActionRequest) -> ActionResult: ...


_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)


def sanitize_adapter_output(value: str) -> str:
    return _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


class _FakeAdapter:
    adapter_name = "fake"

    def __init__(
        self,
        *,
        supported_actions: list[ActionType],
        fail_actions: set[ActionType] | None = None,
        timeout_actions: set[ActionType] | None = None,
    ) -> None:
        self._supported_actions = supported_actions
        self._fail_actions = fail_actions or set()
        self._timeout_actions = timeout_actions or set()
        self._results: dict[str, ActionResult] = {}
        self.execution_count = 0

    def capabilities(self) -> AdapterCapability:
        return AdapterCapability(
            adapter_name=self.adapter_name,
            action_types=self._supported_actions,
            environments=[Environment.STAGING],
        )

    def execute(self, request: ActionRequest) -> ActionResult:
        cached = self._results.get(request.idempotency_key)
        if cached is not None:
            return cached.model_copy(update={"duplicate": True})

        self.execution_count += 1
        action_type = request.action.action_type
        if action_type not in self._supported_actions:
            result = self._result(request, success=False, executed=False, error_code="unsupported_action")
        elif action_type in self._timeout_actions:
            result = self._result(request, success=False, executed=False, error_code="timeout")
        elif action_type in self._fail_actions:
            result = self._result(request, success=False, executed=False, error_code="simulated_failure")
        else:
            result = self._result(
                request,
                success=True,
                executed=not request.dry_run,
                output=f"{self.adapter_name} dry_run={request.dry_run} token=fixture-secret",
            )
        self._results[request.idempotency_key] = result
        return result

    def _result(
        self,
        request: ActionRequest,
        *,
        success: bool,
        executed: bool,
        error_code: str | None = None,
        output: str = "",
    ) -> ActionResult:
        return ActionResult(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            adapter_name=self.adapter_name,
            success=success,
            executed=executed,
            sanitized_output=sanitize_adapter_output(output or error_code or "ok"),
            error_code=error_code,
        )


class FakeDockerAdapter(_FakeAdapter):
    adapter_name = "fake_docker"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            supported_actions=[
                ActionType.READ_HEALTH,
                ActionType.READ_LOGS,
                ActionType.START_CONTAINER,
                ActionType.RESTART_CONTAINER,
                ActionType.STOP_FAULT_INJECTOR,
            ],
            **kwargs,
        )


class FakeAnsibleAdapter(_FakeAdapter):
    adapter_name = "fake_ansible"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            supported_actions=[
                ActionType.READ_HEALTH,
                ActionType.RUN_ANSIBLE_PLAYBOOK,
                ActionType.REMOVE_SCOPED_PORT_BLOCK,
                ActionType.RESTORE_MOODLEDATA_PERMISSION,
            ],
            **kwargs,
        )


class SafeDryRunAdapter:
    """Catalog-bound adapter that proves routing without any side effect.

    It deliberately has no subprocess, SSH, Docker SDK, Ansible, or AWS client
    dependency.  A request is accepted only when the exact scenario/action/
    target tuple exists in the S3.3 execution catalog and its adapter kind
    matches this adapter.
    """

    adapter_kind: str

    def __init__(self, adapter_kind: str, *, catalog: ActionCatalog | None = None) -> None:
        self.adapter_kind = adapter_kind
        self.adapter_name = f"safe_dry_run_{adapter_kind}"
        self._catalog = catalog or ActionCatalog.load_moodle_s3_3_execution_catalog()
        self._results: dict[str, ActionResult] = {}

    def execute(self, request: SafeActionRequest) -> ActionResult:
        cached = self._results.get(request.idempotency_key)
        if cached is not None:
            return cached.model_copy(update={"duplicate": True})

        entry = self._catalog.lookup(request.catalog_action_id)
        allowed = self._catalog.is_safe_execution_allowed(
            request.catalog_action_id,
            scenario_id=request.scenario_id,
            target_scope=request.target_scope,
            environment=request.action.environment.value,
            role="verifier" if entry and entry.permission.read_only else "executor",
        )
        if entry is None or entry.adapter != self.adapter_kind or not allowed:
            result = ActionResult(
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                adapter_name=self.adapter_name,
                success=False,
                executed=False,
                sanitized_output="request rejected by safe dry-run adapter",
                error_code="outside_execution_boundary",
            )
        else:
            result = ActionResult(
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                adapter_name=self.adapter_name,
                success=True,
                executed=False,
                sanitized_output=(
                    "dry-run plan accepted: "
                    f"catalog_action={entry.action_id} target_scope={request.target_scope}"
                ),
            )
        self._results[request.idempotency_key] = result
        return result


class SafeDockerDryRunAdapter(SafeDryRunAdapter):
    def __init__(self, *, catalog: ActionCatalog | None = None) -> None:
        super().__init__("docker", catalog=catalog)


class SafeNetworkDryRunAdapter(SafeDryRunAdapter):
    def __init__(self, *, catalog: ActionCatalog | None = None) -> None:
        super().__init__("network", catalog=catalog)


class SafeAnsibleDryRunAdapter(SafeDryRunAdapter):
    def __init__(self, *, catalog: ActionCatalog | None = None) -> None:
        super().__init__("ansible", catalog=catalog)


class SafeProbeDryRunAdapter(SafeDryRunAdapter):
    def __init__(self, *, catalog: ActionCatalog | None = None) -> None:
        super().__init__("probe", catalog=catalog)
