"""Communication contract domain models and YAML loader for Independent Verification.

Invariants:
1. Versioned contract strictly declaring allowed connections, forbidden connections,
   and required service flows.
2. Read-only verification specification — never imports or depends on any executor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ConnectionRule(BaseModel):
    """Specification of a network connection between two services."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(default="")
    source: str = Field(..., min_length=1)
    destination: str = Field(..., min_length=1)
    port: int = Field(..., ge=0, le=65535)
    protocol: str = Field(default="tcp")
    description: str = Field(default="")


class ServiceDefinition(BaseModel):
    """Service tier definition and required operational flows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(default="")
    health_endpoint: str = Field(default="")
    required_flows: list[str] = Field(default_factory=list)


class CommunicationContract(BaseModel):
    """Versioned communication contract defining network rules and service topologies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(..., min_length=1)
    contract_name: str = Field(..., min_length=1)
    sample_only: bool = Field(
        default=False,
        description="True if this contract is a sample/fixture for mechanism validation rather than Moodle.",
    )
    target_system: str = Field(..., min_length=1)
    description: str = Field(default="")
    services: dict[str, ServiceDefinition] = Field(default_factory=dict)
    allowed_connections: list[ConnectionRule] = Field(default_factory=list)
    forbidden_connections: list[ConnectionRule] = Field(default_factory=list)

    def get_required_flows_for_service(self, service_name: str) -> list[str]:
        svc = self.services.get(service_name)
        return list(svc.required_flows) if svc else []

    def get_allowed_connection(self, conn_id: str) -> ConnectionRule | None:
        for conn in self.allowed_connections:
            if conn.id == conn_id or conn.name == conn_id:
                return conn
        return None

    def get_forbidden_connection(self, conn_id: str) -> ConnectionRule | None:
        for conn in self.forbidden_connections:
            if conn.id == conn_id or conn.name == conn_id:
                return conn
        return None


def load_communication_contract(path: str | Path) -> CommunicationContract:
    """Load and validate a YAML communication contract from disk."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Communication contract file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data: Any = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Contract file at {file_path} did not produce a valid dictionary")

    return CommunicationContract.model_validate(data)

