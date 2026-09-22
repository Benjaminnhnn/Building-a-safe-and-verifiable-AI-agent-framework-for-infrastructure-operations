"""Infrastructure adapter contracts and deterministic fake implementations."""

from __future__ import annotations

import re
from typing import Protocol

from core.schema.action import TypedAction
from core.schema.common import ActionType, Environment
from pydantic import BaseModel, Field


class AdapterCapability(BaseModel):
    adapter_name: str
    action_types: list[ActionType]
    environments: list[Environment]
    target_resource_ids: list[str] = Field(default_factory=list)


class ActionRequest(BaseModel):
    request_id: str
    idempotency_key: str
    action: TypedAction
    dry_run: bool = True
    timeout_seconds: int = Field(default=30, gt=0, le=300)


class ActionResult(BaseModel):
    request_id: str
    idempotency_key: str
    adapter_name: str
    success: bool
    executed: bool
    duplicate: bool = False
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
