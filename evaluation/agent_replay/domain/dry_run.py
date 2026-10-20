"""DryRunner, ReadOnlyTargetClient, and ReadOnlyGuardClient for ActionPlan dry-runs.

Design invariants
-----------------
1. Dry-run must NEVER execute or permit mutating actions on target infrastructure.
2. Read-only safety is enforced at two distinct layers:
   a. Interface layer: ``ReadOnlyTargetClient`` protocol exposes only read operations.
   b. Guard layer: ``ReadOnlyGuardClient`` actively intercepts any attempted mutating
      operation and immediately raises ``MutationNotAllowedError``.
3. Precondition verification:
   - Target current state is retrieved through the read-only client.
   - If target does not exist or any precondition does not hold, the runner returns
     verdict DENY with an explicit explanation and list of failed preconditions.
4. Preview generation:
   - When all preconditions match, runner produces a ``DryRunPreview`` with:
     * Projected state diff (structured changes + unified text diff).
     * Complete list of affected resource IDs from ``expected_scope``.
     * Blast radius, operation, action type, and expected outcome.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.action_plan import ActionPlan, BlastRadius
from evaluation.agent_replay.domain.enums import ActionType


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MutationNotAllowedError(RuntimeError):
    """Raised when an operation that could mutate state is attempted during dry-run."""


# ---------------------------------------------------------------------------
# Decisions & Preview Models
# ---------------------------------------------------------------------------


class DryRunDecision(str, Enum):
    """Verdict returned by DryRunner."""

    ALLOW = "ALLOW"
    DENY = "DENY"


class StateDiff(BaseModel):
    """Structured and human-readable difference between current and projected state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    before: dict[str, Any]
    after: dict[str, Any]
    changes: dict[str, dict[str, Any]] = Field(
        description="Key-level delta mapping: key -> {'old': ..., 'new': ..., 'type': ...}"
    )
    unified_diff: str = Field(
        description="Unified diff text showing formatted changes."
    )


class DryRunPreview(BaseModel):
    """Dry-run impact preview generated when preconditions are satisfied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_hash: str
    target_resource_id: str
    affected_resource_ids: list[str]
    max_blast_radius: BlastRadius
    operation: str
    action_type: ActionType
    expected_outcome: str
    current_state: dict[str, Any]
    projected_state: dict[str, Any]
    diff: StateDiff


class DryRunResult(BaseModel):
    """Result of evaluating an ActionPlan with DryRunner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: DryRunDecision
    reason: str
    plan_hash: str
    target_resource_id: str
    preview: DryRunPreview | None = None
    evaluated_preconditions: list[str] = Field(default_factory=list)
    failed_preconditions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Client Protocol & Guard
# ---------------------------------------------------------------------------


@runtime_checkable
class ReadOnlyTargetClient(Protocol):
    """Protocol for target infrastructure clients that only permit read operations."""

    def get_resource_state(self, resource_id: str) -> dict[str, Any] | None:
        """Fetch the current observed state dictionary for the specified resource."""
        ...


class CallRecord(BaseModel):
    """Audit record of a call made to a guarded client."""

    model_config = ConfigDict(frozen=True)

    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class ReadOnlyGuardClient:
    """Guard wrapper that intercepts calls to an underlying client and strictly
    forbids any mutating methods during dry-run.

    Any attempt to invoke a mutating method raises ``MutationNotAllowedError``
    immediately.  Permitted read-only calls are recorded in ``call_history``.
    """

    MUTATING_VERBS = frozenset({
        "restart", "stop", "start", "kill", "terminate", "reboot",
        "execute", "exec", "run_command", "write", "modify", "update",
        "delete", "remove", "apply", "create", "set", "patch", "deploy",
        "recreate", "rollback", "mutate", "inject", "drop", "truncate",
    })

    ALLOWED_READ_PREFIXES = (
        "get_", "read_", "describe_", "list_", "inspect_", "fetch_",
        "check_", "is_", "has_", "probe_", "query_", "search_", "view_",
    )

    ALLOWED_READ_METHODS = frozenset({
        "get_resource_state", "get_status", "read_config", "inspect_resource",
        "query_state", "check_health",
    })

    def __init__(self, target_client: Any) -> None:
        self._target_client = target_client
        self._call_history: list[CallRecord] = []
        self._mutation_attempts: list[str] = []

    @property
    def call_history(self) -> list[CallRecord]:
        return list(self._call_history)

    @property
    def mutation_attempts(self) -> list[str]:
        return list(self._mutation_attempts)

    def has_mutations(self) -> bool:
        """Returns True if any mutation succeeded. Always False because mutations are blocked."""
        return False

    def _is_mutating_method(self, name: str) -> bool:
        lower_name = name.lower()
        # Direct verb match
        if lower_name in self.MUTATING_VERBS:
            return True
        # Prefix or substring match with common mutating verbs
        for verb in self.MUTATING_VERBS:
            if lower_name.startswith(f"{verb}_") or lower_name.endswith(f"_{verb}"):
                return True
        return False

    def _is_read_method(self, name: str) -> bool:
        if name in self.ALLOWED_READ_METHODS:
            return True
        for prefix in self.ALLOWED_READ_PREFIXES:
            if name.startswith(prefix):
                return True
        return False

    def get_resource_state(self, resource_id: str) -> dict[str, Any] | None:
        """Permitted read operation delegated to underlying client."""
        self._call_history.append(
            CallRecord(method="get_resource_state", args=(resource_id,), kwargs={})
        )
        if hasattr(self._target_client, "get_resource_state"):
            return self._target_client.get_resource_state(resource_id)
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return getattr(self._target_client, name)

        if not hasattr(self._target_client, name):
            raise AttributeError(f"Client has no attribute '{name}'")

        attr = getattr(self._target_client, name)
        if not callable(attr):
            return attr

        if self._is_mutating_method(name):
            def blocked_mutation(*args: Any, **kwargs: Any) -> Any:
                self._mutation_attempts.append(name)
                raise MutationNotAllowedError(
                    f"Mutating operation '{name}' is strictly forbidden during dry-run."
                )
            return blocked_mutation

        if self._is_read_method(name):
            def recorded_read(*args: Any, **kwargs: Any) -> Any:
                self._call_history.append(CallRecord(method=name, args=args, kwargs=kwargs))
                return attr(*args, **kwargs)
            return recorded_read

        # Fail-closed default: unrecognized callable methods are treated as unsafe
        def unknown_blocked(*args: Any, **kwargs: Any) -> Any:
            self._mutation_attempts.append(name)
            raise MutationNotAllowedError(
                f"Operation '{name}' is not an approved read-only method and is blocked during dry-run."
            )
        return unknown_blocked


class DictReadOnlyClient:
    """In-memory read-only target client for testing and simulation."""

    def __init__(self, resources: dict[str, dict[str, Any]] | None = None) -> None:
        self._resources: dict[str, dict[str, Any]] = copy.deepcopy(resources) if resources else {}
        self.read_calls: list[str] = []

    def get_resource_state(self, resource_id: str) -> dict[str, Any] | None:
        self.read_calls.append(resource_id)
        state = self._resources.get(resource_id)
        return copy.deepcopy(state) if state is not None else None


# ---------------------------------------------------------------------------
# Precondition Evaluation Helpers
# ---------------------------------------------------------------------------

_PRECONDITION_OP_RE = re.compile(
    r"^\s*([a-zA-Z0-9_\.\-]+)\s*(==|!=|<=|>=|<|>|=|:|\bin\b|\bnot in\b)\s*(.+?)\s*$"
)


def _parse_value(val_str: str) -> Any:
    val_str = val_str.strip()
    # Quotes
    if (val_str.startswith('"') and val_str.endswith('"')) or (
        val_str.startswith("'") and val_str.endswith("'")
    ):
        return val_str[1:-1]
    # Boolean
    if val_str.lower() == "true":
        return True
    if val_str.lower() == "false":
        return False
    if val_str.lower() == "none" or val_str.lower() == "null":
        return None
    # Int
    try:
        return int(val_str)
    except ValueError:
        pass
    # Float
    try:
        return float(val_str)
    except ValueError:
        pass
    # List or tuple format
    if (val_str.startswith("[") and val_str.endswith("]")) or (
        val_str.startswith("(") and val_str.endswith(")")
    ):
        inner = val_str[1:-1]
        items = [_parse_value(item.strip()) for item in inner.split(",") if item.strip()]
        return items
    return val_str


def evaluate_precondition(
    condition: str,
    target_state: dict[str, Any],
    *,
    target_resource_id: str = "",
) -> tuple[bool, str]:
    """Evaluate a single precondition string against target state.

    Returns (is_satisfied, failure_reason).
    """
    cond_clean = condition.strip()
    if not cond_clean:
        return True, ""

    # Natural language checks for common clauses
    lower_cond = cond_clean.lower()
    if lower_cond in ("target resource exists", "target exists", "resource exists"):
        if target_state is not None:
            return True, ""
        return False, f"target resource '{target_resource_id}' does not exist"

    if lower_cond == "environment is staging":
        env = target_state.get("environment") or target_state.get("env")
        if env == "staging":
            return True, ""
        return False, f"expected environment 'staging', observed {env!r}"

    # Match operator expression
    match = _PRECONDITION_OP_RE.match(cond_clean)
    if match:
        key, op, raw_val = match.groups()
        expected = _parse_value(raw_val)
        actual = target_state.get(key)

        if key not in target_state:
            return False, f"state key '{key}' not found in target state"

        if op in ("==", "="):
            if str(actual).lower() == str(expected).lower() or actual == expected:
                return True, ""
            return False, f"expected {key} == {expected!r}, observed {actual!r}"

        if op == ":":
            if str(actual).lower() == str(expected).lower() or actual == expected:
                return True, ""
            return False, f"expected {key}: {expected!r}, observed {actual!r}"

        if op == "!=":
            if str(actual).lower() != str(expected).lower() and actual != expected:
                return True, ""
            return False, f"expected {key} != {expected!r}, but was equal"

        if op in ("<", "<=", ">", ">="):
            try:
                num_actual = float(actual)
                num_expected = float(expected)
            except (TypeError, ValueError):
                return False, f"numeric comparison failed: '{actual}' vs '{expected}'"
            if op == "<" and num_actual < num_expected:
                return True, ""
            if op == "<=" and num_actual <= num_expected:
                return True, ""
            if op == ">" and num_actual > num_expected:
                return True, ""
            if op == ">=" and num_actual >= num_expected:
                return True, ""
            return False, f"condition '{key} {op} {expected}' failed (observed {actual})"

        if op == "in":
            expected_list = expected if isinstance(expected, (list, tuple, set)) else [expected]
            if actual in expected_list or str(actual) in [str(x) for x in expected_list]:
                return True, ""
            return False, f"observed {actual!r} not in {expected_list!r}"

        if op == "not in":
            expected_list = expected if isinstance(expected, (list, tuple, set)) else [expected]
            if actual not in expected_list and str(actual) not in [str(x) for x in expected_list]:
                return True, ""
            return False, f"observed {actual!r} unexpectedly found in {expected_list!r}"

    # Negation flag check (e.g. "not locked")
    if lower_cond.startswith("not "):
        flag_key = cond_clean[4:].strip()
        actual = target_state.get(flag_key)
        if not actual:
            return True, ""
        return False, f"expected not '{flag_key}', but value was truthy ({actual!r})"

    # Direct truthy key check (e.g. "in_maintenance_window")
    if cond_clean in target_state:
        actual = target_state[cond_clean]
        if actual:
            return True, ""
        return False, f"flag '{cond_clean}' is not truthy in target state (observed {actual!r})"

    # If the condition doesn't match any known pattern and is not in state
    return False, f"precondition '{cond_clean}' could not be verified against target state"


# ---------------------------------------------------------------------------
# Default State Projector
# ---------------------------------------------------------------------------


def _default_state_projector(plan: ActionPlan, current_state: dict[str, Any]) -> dict[str, Any]:
    """Predict state changes resulting from applying plan's action_type/operation."""
    projected = copy.deepcopy(current_state)
    atype = plan.action_type

    if atype == ActionType.RESTART_CONTAINER:
        projected["status"] = "running"
        projected["restarted"] = True
    elif atype == ActionType.START_CONTAINER:
        projected["status"] = "running"
    elif atype == ActionType.STOP_FAULT_INJECTOR:
        projected["status"] = "stopped"
        projected["fault_active"] = False
    elif atype == ActionType.REMOVE_SCOPED_PORT_BLOCK:
        projected["port_blocked"] = False
        projected["connectivity"] = "restored"
    elif atype == ActionType.RESTORE_MOODLEDATA_PERMISSION:
        projected["permissions"] = "0775"
        projected["ownership"] = "www-data:www-data"
    elif atype == ActionType.NETWORK_RECONNECT:
        projected["network_connected"] = True
    elif atype == ActionType.RECONFIGURE_SERVICE:
        projected["config_synced"] = True
    elif atype == ActionType.RUN_ANSIBLE_PLAYBOOK:
        projected["playbook_applied"] = True
    elif atype in (ActionType.READ_HEALTH, ActionType.READ_LOGS):
        # Read operations do not mutate state
        pass
    else:
        projected["last_action"] = atype.value

    return projected


# ---------------------------------------------------------------------------
# Diff Construction
# ---------------------------------------------------------------------------


def _compute_state_diff(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    target_resource_id: str,
) -> StateDiff:
    """Compute structured delta and unified text diff between before and after states."""
    changes: dict[str, dict[str, Any]] = {}
    all_keys = sorted(set(before.keys()) | set(after.keys()))

    for key in all_keys:
        if key not in before:
            changes[key] = {"old": None, "new": after[key], "type": "added"}
        elif key not in after:
            changes[key] = {"old": before[key], "new": None, "type": "removed"}
        elif before[key] != after[key]:
            changes[key] = {"old": before[key], "new": after[key], "type": "modified"}

    before_lines = [
        f"{line}\n" for line in json.dumps(before, indent=2, sort_keys=True).splitlines()
    ]
    after_lines = [
        f"{line}\n" for line in json.dumps(after, indent=2, sort_keys=True).splitlines()
    ]

    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{target_resource_id} (current)",
            tofile=f"b/{target_resource_id} (projected)",
        )
    )
    unified_diff_str = "".join(diff_lines)

    return StateDiff(
        before=before,
        after=after,
        changes=changes,
        unified_diff=unified_diff_str,
    )


# ---------------------------------------------------------------------------
# DryRunner Implementation
# ---------------------------------------------------------------------------


class DryRunner:
    """Safely evaluates an ActionPlan against target infrastructure without mutating state."""

    def __init__(
        self,
        *,
        default_projector: Callable[[ActionPlan, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._projector = default_projector or _default_state_projector

    def run(
        self,
        plan: ActionPlan,
        client: ReadOnlyTargetClient | Any,
        *,
        state_delta: dict[str, Any] | None = None,
    ) -> DryRunResult:
        """Execute a dry-run evaluation of the given ActionPlan.

        Steps:
        1. Access the target resource through a read-only guard.
        2. Evaluate all preconditions against the current state.
        3. If any precondition fails or target is missing, return DENY.
        4. If all preconditions pass, compute projected state, diff, and affected scope.
        """
        if not isinstance(plan, ActionPlan):
            raise TypeError(f"plan must be an instance of ActionPlan, got {type(plan).__name__}")

        # Enforce read-only safety via guard wrapper
        guarded_client = (
            client if isinstance(client, ReadOnlyGuardClient) else ReadOnlyGuardClient(client)
        )

        target_id = plan.target_resource_id
        current_state = guarded_client.get_resource_state(target_id)

        # Target must exist
        if current_state is None:
            return DryRunResult(
                decision=DryRunDecision.DENY,
                reason=f"Target resource '{target_id}' was not found in observed infrastructure.",
                plan_hash=plan.plan_hash,
                target_resource_id=target_id,
                preview=None,
                evaluated_preconditions=list(plan.preconditions),
                failed_preconditions=[f"target_exists: {target_id}"],
            )

        # Evaluate preconditions
        failed_preconditions: list[str] = []
        failure_details: list[str] = []

        for cond in plan.preconditions:
            ok, detail = evaluate_precondition(
                cond, current_state, target_resource_id=target_id
            )
            if not ok:
                failed_preconditions.append(cond)
                failure_details.append(detail)

        if failed_preconditions:
            return DryRunResult(
                decision=DryRunDecision.DENY,
                reason=f"Precondition mismatch: {'; '.join(failure_details)}",
                plan_hash=plan.plan_hash,
                target_resource_id=target_id,
                preview=None,
                evaluated_preconditions=list(plan.preconditions),
                failed_preconditions=failed_preconditions,
            )

        # Preconditions match -> Project state and generate diff preview
        projected_state = self._projector(plan, current_state)
        if state_delta:
            projected_state.update(copy.deepcopy(state_delta))

        diff = _compute_state_diff(current_state, projected_state, target_resource_id=target_id)

        preview = DryRunPreview(
            plan_hash=plan.plan_hash,
            target_resource_id=target_id,
            affected_resource_ids=list(plan.expected_scope.affected_resource_ids),
            max_blast_radius=plan.expected_scope.max_blast_radius,
            operation=plan.operation,
            action_type=plan.action_type,
            expected_outcome=plan.expected_outcome,
            current_state=current_state,
            projected_state=projected_state,
            diff=diff,
        )

        return DryRunResult(
            decision=DryRunDecision.ALLOW,
            reason="All preconditions satisfied. Dry-run preview generated successfully.",
            plan_hash=plan.plan_hash,
            target_resource_id=target_id,
            preview=preview,
            evaluated_preconditions=list(plan.preconditions),
            failed_preconditions=[],
        )
