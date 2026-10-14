from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from urllib.request import Request

from core import safe_executor_client as client


def test_action_digest_is_bound_to_staging_action() -> None:
    first = client.action_digest("DB-01", "remove_scoped_db_reject", "staging_moodle_nodes", "run-0001-unique-key")
    assert len(first) == 64
    assert first != client.action_digest("RES-01", "remove_scoped_db_reject", "staging_moodle_nodes", "run-0001-unique-key")
    assert first != client.action_digest("DB-01", "remove_scoped_db_reject", "production", "run-0001-unique-key")
    assert first != client.action_digest("DB-01", "remove_scoped_db_reject", "staging_moodle_nodes", "run-0002-unique-key")


def test_client_signs_exact_payload_and_sends_approval(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"status":"disabled"}'

        @property
        def headers(self):
            body = self.read()
            return {"X-Executor-Response-Signature": client._response_signature(200, body, b"a" * 64)}

    def fake_urlopen(request: Request, timeout: float):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("SAFE_EXECUTOR_URL", "http://host.docker.internal:8765")
    monkeypatch.setenv("SAFE_EXECUTOR_HMAC_KEY", "a" * 64)
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    idem = "drill-20260929-db01-0001"
    approval = {"actor_id": "operator", "expires_at": "later", "action_sha256": "a" * 64, "signature": "sig"}
    result = client.execute_approved_staging_action(
        scenario_id="DB-01",
        catalog_action_id="remove_scoped_db_reject",
        target_scope="staging_moodle_nodes",
        idempotency_key=idem,
        approval=approval,
        timeout_seconds=4,
    )
    request = captured["request"]
    assert isinstance(request, Request)
    payload = json.loads(request.data)
    assert payload["approval"] == approval
    assert payload["action_sha256"] == client.action_digest("DB-01", "remove_scoped_db_reject", "staging_moodle_nodes", idem)
    signed = (
        b"POST\n/v1/execute\n"
        + request.get_header("X-executor-timestamp").encode()
        + b"\n"
        + request.get_header("X-executor-nonce").encode()
        + b"\n"
        + hashlib.sha256(request.data).hexdigest().encode()
    )
    expected = hmac.new(b"a" * 64, signed, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, request.get_header("X-executor-signature"))
    assert captured["timeout"] == 4
    assert result == {"status": "disabled"}


def test_client_reads_transport_key_from_mounted_secret_file(monkeypatch, tmp_path: Path) -> None:
    secret_file = tmp_path / "executor-hmac"
    secret_file.write_text("b" * 64, encoding="ascii")
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"status":"disabled"}'

        @property
        def headers(self):
            body = self.read()
            return {"X-Executor-Response-Signature": client._response_signature(200, body, b"b" * 64)}

    def fake_urlopen(request: Request, timeout: float):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("SAFE_EXECUTOR_URL", "http://host.docker.internal:8765")
    monkeypatch.delenv("SAFE_EXECUTOR_HMAC_KEY", raising=False)
    monkeypatch.setenv("SAFE_EXECUTOR_HMAC_KEY_FILE", str(secret_file))
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    client.execute_approved_staging_action(
        scenario_id="DB-01",
        catalog_action_id="remove_scoped_db_reject",
        target_scope="staging_moodle_nodes",
        idempotency_key="mounted-secret-test-0001",
        approval={"actor_id": "operator", "expires_at": "later", "action_sha256": "x", "signature": "y"},
    )
    assert captured["timeout"] == 105.0
    request = captured["request"]
    payload = json.loads(request.data)
    signed = (
        b"POST\n/v1/execute\n"
        + request.get_header("X-executor-timestamp").encode()
        + b"\n"
        + request.get_header("X-executor-nonce").encode()
        + b"\n"
        + hashlib.sha256(request.data).hexdigest().encode()
    )
    expected = hmac.new(b"b" * 64, signed, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, request.get_header("X-executor-signature"))
    assert payload["environment"] == "staging"


def test_client_rejects_unsigned_or_tampered_executor_response(monkeypatch) -> None:
    class Response:
        status = 200
        headers = {"X-Executor-Response-Signature": "0" * 64}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"status":"executed","mutated":true}'

    monkeypatch.setenv("SAFE_EXECUTOR_URL", "http://host.docker.internal:8765")
    monkeypatch.setenv("SAFE_EXECUTOR_HMAC_KEY", "c" * 64)
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    import pytest

    with pytest.raises(RuntimeError, match="response authentication failed"):
        client.execute_approved_staging_action(
            scenario_id="DB-01",
            catalog_action_id="remove_scoped_db_reject",
            target_scope="staging_moodle_nodes",
            idempotency_key="tampered-response-test",
            approval={"actor_id": "operator", "expires_at": "later", "action_sha256": "x", "signature": "y"},
        )
