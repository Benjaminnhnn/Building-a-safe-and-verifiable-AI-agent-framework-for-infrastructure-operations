"""Closed enumerations for the AIOps domain layer.

Rules enforced here:
- ActionType is a closed set; arbitrary shell commands are not a valid type.
- BLOCKED_UNRESTRICTED_SHELL exists as an explicit, permanently-rejected
  sentinel so that any attempt to propose a shell action is categorised and
  denied by the Safety Gate rather than silently falling through.
- EvidenceSource captures the provenance of every evidence record.
- Environment restricts execution targets to known deployment environments.
"""

from __future__ import annotations

from enum import Enum


class Environment(str, Enum):
    """Deployment environment.  Execution is only permitted in STAGING for
    the thesis scope; PRODUCTION requires HUMAN_ONLY approval."""

    STAGING = "staging"
    PRODUCTION = "production"


class EvidenceSource(str, Enum):
    """Provenance of an evidence record.

    Values map to the collection mechanism, not the storage format:
    - METRIC   : time-series value from Prometheus or similar
    - LOG      : structured or plain-text log excerpt
    - CONFIG   : configuration file snapshot or diff
    - RUNBOOK  : knowledge-base / RAG retrieval used as advisory context
    - DEPENDENCY : topology/dependency-graph inference
    """

    METRIC = "metric"
    LOG = "log"
    CONFIG = "config"
    RUNBOOK = "runbook"
    DEPENDENCY = "dependency"


class ActionType(str, Enum):
    """Closed set of permitted action types.

    No member of this enum executes arbitrary shell commands.
    BLOCKED_UNRESTRICTED_SHELL is a permanent DENY sentinel — it can be
    proposed by a broken planner but must be rejected by the Safety Gate
    before any executor receives it.
    """

    READ_HEALTH = "read_health"
    READ_LOGS = "read_logs"
    RESTART_CONTAINER = "restart_container"
    START_CONTAINER = "start_container"
    STOP_FAULT_INJECTOR = "stop_fault_injector"
    REMOVE_SCOPED_PORT_BLOCK = "remove_scoped_port_block"
    RESTORE_MOODLEDATA_PERMISSION = "restore_moodledata_permission"
    RUN_ANSIBLE_PLAYBOOK = "run_ansible_playbook"
    RECONFIGURE_SERVICE = "reconfigure_service"
    NETWORK_RECONNECT = "network_reconnect"
    # Permanent DENY sentinel — never executable
    BLOCKED_UNRESTRICTED_SHELL = "blocked_unrestricted_shell"

