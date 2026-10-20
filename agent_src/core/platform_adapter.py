"""Platform adapter contract and registry for domain-neutral incident operations.

This module defines the platform-neutral adapter interface and registry.
The shared core coordinates incident workflows exclusively through these
abstractions without any direct dependencies on specific platforms (e.g. Moodle).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from core.safety.action_catalog import ActionCatalog
from core.schema.resource import Resource

logger = logging.getLogger(__name__)


@runtime_checkable
class PlatformAdapter(Protocol):
    """Platform adapter contract for domain-neutral incident operations."""

    @property
    def platform_name(self) -> str:
        """Identifier of the platform (e.g. 'moodle', 'erpnext', 'mock')."""
        ...

    def can_handle(self, alert: dict[str, Any]) -> bool:
        """Return True if this adapter can process the given Alertmanager alert."""
        ...

    def identify_resource(self, alert: dict[str, Any]) -> str | None:
        """Resolve an alert's labels/annotations to a canonical resource_id."""
        ...

    def get_resources(self) -> list[Resource]:
        """Return the inventory of known infrastructure resources."""
        ...

    def get_resource(self, resource_id: str) -> Resource | None:
        """Look up a specific resource by ID."""
        ...

    def get_resource_types(self) -> dict[str, str]:
        """Return mapping of resource_id -> type string."""
        ...

    def get_alert_queries(self, alert_name: str) -> tuple[tuple[str, str], ...]:
        """Return metric query pairs (metric_id, PromQL) relevant to the alert."""
        ...

    def collect_evidence(
        self,
        alert: dict[str, Any],
        *,
        incident_id: str,
        resource_id: str,
        evidence_store: Any,
        prometheus_url: str | None = None,
    ) -> dict[str, Any]:
        """Collect bounded runtime observations/metrics for the alert."""
        ...

    def diagnose(self, alert_name: str, evidence: list[Any]) -> dict[str, Any]:
        """Perform conservative, rule-based diagnosis on collected evidence."""
        ...

    def get_action_catalog(self) -> ActionCatalog:
        """Return the allowlisted action catalog for this platform."""
        ...

    def plan_investigation(
        self, incident_id: str, diagnosis: dict[str, Any]
    ) -> Any | None:
        """Propose safe, read-only investigation probes."""
        ...

    def plan_remediation(
        self,
        incident_id: str,
        alert_name: str,
        diagnosis: dict[str, Any],
        evidence: list[Any],
    ) -> dict[str, Any] | None:
        """Propose a safe staging remediation candidate for human review."""
        ...

    def get_target_scopes(self) -> dict[str, set[str]]:
        """Return target scope names mapped to sets of allowed resource IDs."""
        ...

    def get_verification_probes(
        self,
        incident_id: str,
        *,
        scenario_id: str | None = None,
        action_id: str | None = None,
    ) -> dict[str, list[Any]]:
        """Return verification probe sets (allowed, forbidden, related)."""
        ...

class PlatformAdapterRegistry:
    """Registry of platform adapters allowing dynamic discovery and decoupling."""

    _adapters: dict[str, PlatformAdapter] = {}

    @classmethod
    def register(cls, adapter: PlatformAdapter) -> None:
        """Register a platform adapter instance."""
        cls._adapters[adapter.platform_name] = adapter

    @classmethod
    def unregister(cls, name: str) -> None:
        """Unregister an adapter by platform name."""
        cls._adapters.pop(name, None)

    @classmethod
    def get(cls, name: str) -> PlatformAdapter | None:
        """Look up an adapter by name. Discovers core.<name>_adapter dynamically."""
        if name not in cls._adapters:
            try:
                import importlib

                try:
                    importlib.import_module(f"core.{name}.adapter")
                except (ImportError, ModuleNotFoundError):
                    importlib.import_module(f"core.{name}_adapter")
            except (ImportError, ModuleNotFoundError):
                pass
        return cls._adapters.get(name)

    @classmethod
    def find_for_alert(cls, alert: dict[str, Any]) -> PlatformAdapter | None:
        """Find the first registered adapter that can handle this alert."""
        for adapter in cls._adapters.values():
            if adapter.can_handle(alert):
                return adapter
        # Try dynamic discovery for known platforms
        for name in ("moodle", "erpnext"):
            adapter = cls.get(name)
            if adapter and adapter.can_handle(alert):
                return adapter
        return None

    @classmethod
    def all_adapters(cls) -> list[PlatformAdapter]:
        """Return all currently registered adapters."""
        return list(cls._adapters.values())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered adapters (useful for unit tests)."""
        cls._adapters.clear()

