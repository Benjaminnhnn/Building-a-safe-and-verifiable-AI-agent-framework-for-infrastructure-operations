"""Hermes Agent integration package.

Provides:
- Hermes MCP server (read-only telemetry and proposal generation).
- Lifecycle hooks (pre_tool_call interception and multi-AI task rotation).
"""

from .hooks import HermesSafetyHook, validate_multi_ai_task_rotation
from .mcp_server import HermesMcpServer

__all__ = [
    "HermesMcpServer",
    "HermesSafetyHook",
    "validate_multi_ai_task_rotation",
]
