"""Redaction helpers shared by evidence collectors and domain contracts."""

from __future__ import annotations

import re
import json
import math
from typing import Any


_SECRET_NAME = (
    r"[A-Za-z0-9_-]*(?:password|passwd|pwd|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|authorization|credential|"
    r"connection[_-]?string|dsn)[A-Za-z0-9_-]*"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?<![A-Za-z0-9])({_SECRET_NAME})(\s*[:=]\s*)((?:Bearer\s+)?[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
_URL_CREDENTIALS = re.compile(r"(://)[^/@\s]+:[^/@\s]+@")
_SECRET_FIELD = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|credential|connection[_-]?string|dsn)"
)
_REDACTION_MARKERS = {
    "[REDACTED]",
    "[REDACTED_PRIVATE_KEY]",
    "[REDACTED_AWS_ACCESS_KEY]",
}


def sanitize_log_excerpt(value: Any, *, max_chars: int = 1024) -> str:
    """Redact common credentials and bound copied log context."""
    text = str(value or "")
    text = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _AWS_ACCESS_KEY.sub("[REDACTED_AWS_ACCESS_KEY]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    return text[:max_chars]


def sanitize_untrusted_data(
    value: Any,
    *,
    max_chars: int = 1024,
    max_items: int = 100,
    max_depth: int = 8,
    _depth: int = 0,
) -> Any:
    """Redact and bound JSON-like untrusted data while preserving its shape."""
    if _depth >= max_depth:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return sanitize_log_excerpt(value, max_chars=max_chars)
    if isinstance(value, dict):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= max_items:
                result["[TRUNCATED]"] = True
                break
            safe_key = sanitize_log_excerpt(key, max_chars=128)
            if _SECRET_FIELD.search(str(key)):
                result[safe_key] = "[REDACTED]"
            else:
                result[safe_key] = sanitize_untrusted_data(
                    child,
                    max_chars=max_chars,
                    max_items=max_items,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                )
        return result
    if isinstance(value, (list, tuple)):
        items = [
            sanitize_untrusted_data(
                item,
                max_chars=max_chars,
                max_items=max_items,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            items.append("[TRUNCATED]")
        return items
    if isinstance(value, float) and not math.isfinite(value):
        return "[INVALID_NUMBER]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_log_excerpt(value, max_chars=max_chars)


def contains_unredacted_secret(value: str) -> bool:
    """Return whether the shared redactor would change this evidence value."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    else:
        def inspect(item: Any) -> bool:
            if isinstance(item, dict):
                for key, child in item.items():
                    if (
                        _SECRET_FIELD.search(str(key))
                        and isinstance(child, str)
                        and child not in _REDACTION_MARKERS
                    ):
                        return True
                    if inspect(child):
                        return True
                return False
            if isinstance(item, list):
                return any(inspect(child) for child in item)
            if isinstance(item, str):
                try:
                    nested = json.loads(item)
                except ValueError:
                    nested = None
                if isinstance(nested, (dict, list)):
                    return inspect(nested)
                return sanitize_log_excerpt(item, max_chars=len(item) + 1) != item
            return False

        return inspect(parsed)
    return sanitize_log_excerpt(value, max_chars=len(value) + 1) != value
