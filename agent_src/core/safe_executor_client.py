"""Explicit client for the separately gated staging safe-executor API.

This module is deliberately not called by the alert pipeline. A caller must
provide an independently signed, action-bound human approval for each request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _response_signature(status: int, body: bytes, key: bytes) -> str:
    message = b"RESPONSE\n" + str(status).encode() + b"\n" + hashlib.sha256(body).hexdigest().encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _read_signed_response(response: Any, key: bytes) -> dict[str, Any]:
    body = response.read()
    status = response.status if hasattr(response, "status") else response.code
    signature = response.headers.get("X-Executor-Response-Signature", "")
    expected = _response_signature(status, body, key)
    if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        raise RuntimeError("safe executor response authentication failed")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise RuntimeError("safe executor response must be a JSON object")
    return value


def action_digest(scenario_id: str, catalog_action_id: str, target_scope: str, idempotency_key: str) -> str:
    immutable_action = {
        "environment": "staging",
        "scenario_id": scenario_id,
        "catalog_action_id": catalog_action_id,
        "target_scope": target_scope,
        "idempotency_key": idempotency_key,
    }
    return hashlib.sha256(_canonical(immutable_action)).hexdigest()


def execute_approved_staging_action(
    *,
    scenario_id: str,
    catalog_action_id: str,
    target_scope: str,
    idempotency_key: str,
    approval: dict[str, str],
    # The API runs fixed SSH actions sequentially on at most two nodes, with a
    # 45-second subprocess limit per node. Leave a response margin so clients
    # do not abandon an action the API may still be executing.
    timeout_seconds: float = 105.0,
) -> dict[str, Any]:
    """Request one explicitly approved catalog action; never invent approval.

    Required environment: ``SAFE_EXECUTOR_URL`` and either
    ``SAFE_EXECUTOR_HMAC_KEY_FILE`` or ``SAFE_EXECUTOR_HMAC_KEY``. The HMAC key
    is a transport credential, not an approval credential.
    """
    endpoint = os.environ.get("SAFE_EXECUTOR_URL", "").rstrip("/")
    key_file = os.environ.get("SAFE_EXECUTOR_HMAC_KEY_FILE", "")
    key_text = os.environ.get("SAFE_EXECUTOR_HMAC_KEY", "")
    if key_file and key_text:
        raise RuntimeError("configure only one safe executor HMAC key source")
    try:
        key = Path(key_file).read_text(encoding="ascii").strip().encode() if key_file else key_text.encode()
    except OSError as exc:
        raise RuntimeError("safe executor transport credential file is unavailable") from exc
    if not endpoint.startswith("http://") or len(key) < 32:
        raise RuntimeError("safe executor endpoint or transport credential is not configured")
    digest = action_digest(scenario_id, catalog_action_id, target_scope, idempotency_key)
    payload = {
        "scenario_id": scenario_id,
        "catalog_action_id": catalog_action_id,
        "target_scope": target_scope,
        "environment": "staging",
        "action_sha256": digest,
        "idempotency_key": idempotency_key,
        "approval": approval,
    }
    raw = _canonical(payload)
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    signed = b"POST\n/v1/execute\n" + timestamp.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
    signature = hmac.new(key, signed, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        endpoint + "/v1/execute",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Executor-Timestamp": timestamp,
            "X-Executor-Nonce": nonce,
            "X-Executor-Signature": signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return _read_signed_response(response, key)
    except urllib.error.HTTPError as exc:
        return _read_signed_response(exc, key)
