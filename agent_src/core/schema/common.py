# Enum dùng chung
# KHÔNG PHẢI PLACEHOLDER:
# Các giá trị bên dưới là bộ từ khóa cố định của hệ thống.
# Không thay theo từng máy EC2, Docker Compose, Prometheus hay scenario.
# Chỉ sửa khi bạn thật sự đổi thiết kế schema hoặc bổ sung loại trạng thái/hành động mới.

from enum import Enum 

class Environment(str, Enum):
    STAGING = "staging"
    PRODUCTION = "production"

class ResourceType(str, Enum):
    APPLICATION = "application"
    DATABASE = "database"
    CONTAINER = "container"
    ENDPOINT = "endpoint"
    HOST = "host"
    NETWORK = "network"
    VOLUME = "volume"

class IncidentStatus(str, Enum):
    OPEN = "open"
    TRIAGED = "triaged"
    PLANNED = "planned"
    GATED = "gated"
    EXECUTED = "executed"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    FAILED = "failed"

class ActionDecision(str, Enum):
    ALLOW = "allow" 
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    HUMAN_ONLY = "human_only"

class ActionType(str, Enum):
    READ_HEALTH = "read_health"
    READ_LOGS = "read_logs"
    RESTART_CONTAINER = "restart_container"
    START_CONTAINER = "start_container"
    STOP_FAULT_INJECTOR = "stop_fault_injector"
    REMOVE_SCOPED_PORT_BLOCK = "remove_scoped_port_block"
    RESTORE_MOODLEDATA_PERMISSION = "restore_moodledata_permission"
    RUN_ANSIBLE_PLAYBOOK = "run_ansible_playbook"
    BLOCKED_UNRESTRICTED_SHELL = "blocked_unrestricted_shell"


