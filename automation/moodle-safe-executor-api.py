#!/usr/bin/env python3
"""Authenticated staging API for the constrained Moodle reset executor.

The endpoint accepts only catalogued reset actions. It cannot run arbitrary
commands; all remote work uses the forced-command SSH key provisioned by
configure-moodle-safe-executor.yml. Execution remains disabled by default.
"""

from __future__ import annotations

import hashlib
import base64
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PORT = int(os.environ.get("EXECUTOR_PORT", "8765"))
HOST = os.environ.get("EXECUTOR_BIND", "127.0.0.1")
STATE_DB = Path(os.environ["EXECUTOR_STATE_DB"])
SSH_KEY = Path(os.environ["EXECUTOR_SSH_KEY"])
KNOWN_HOSTS = Path(os.environ["EXECUTOR_KNOWN_HOSTS"])
AGENT_KEY = Path(os.environ["EXECUTOR_AGENT_KEY_FILE"]).read_bytes().strip()
APPROVAL_PUBLIC_KEY = Path(os.environ["EXECUTOR_APPROVAL_PUBLIC_KEY_FILE"])
NODES = {"a": os.environ["MOODLE_NODE_A_IP"], "b": os.environ["MOODLE_NODE_B_IP"]}
MAX_BODY = 16_384
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")

# Exact staging catalog: values are remote node aliases, never caller input.
CATALOG = {
    ("DB-01", "remove_scoped_db_reject", "staging_moodle_nodes"): ("a", "b"),
    ("RES-01", "remove_named_cpu_load_container", "moodle-app-b"): ("b",),
    ("NET-01", "recreate_moodle_web_from_reviewed_compose", "staging_moodle_nodes"): ("a", "b"),
    ("CON-01", "start_reviewed_compose_service", "moodle-app-b"): ("b",),
    ("SEC-02", "restore_fixture_directory_mode", "moodledata_synthetic_fixture_directory"): ("a",),
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def action_digest(scenario: str, action_id: str, scope: str, idempotency_key: str) -> str:
    return hashlib.sha256(canonical({
        "environment": "staging",
        "scenario_id": scenario,
        "catalog_action_id": action_id,
        "target_scope": scope,
        "idempotency_key": idempotency_key,
    })).hexdigest()


def init_db() -> None:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(STATE_DB) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS executions (
            idempotency_key TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL,
            status TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL)""")
        db.execute("CREATE TABLE IF NOT EXISTS request_nonces (nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")


def valid_approval(approval: object, digest: str) -> bool:
    if not isinstance(approval, dict) or set(approval) != {"actor_id", "expires_at", "action_sha256", "signature"}:
        return False
    actor, expires_at, signature = approval["actor_id"], approval["expires_at"], approval["signature"]
    if actor not in {"operator", "human-approver"} or not all(isinstance(x, str) for x in (expires_at, signature)):
        return False
    if approval["action_sha256"] != digest:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            return False
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    if expiry <= now or expiry > now + timedelta(minutes=15):
        return False
    signed = f"{digest}:{actor}:{expires_at}".encode()
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
        with tempfile.TemporaryDirectory(prefix="moodle-executor-approval-") as directory:
            signature_path = Path(directory) / "approval.sig"
            message_path = Path(directory) / "approval.txt"
            signature_path.write_bytes(signature_bytes)
            message_path.write_bytes(signed)
            result = subprocess.run(
                ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(APPROVAL_PUBLIC_KEY),
                 "-rawin", "-in", str(message_path), "-sigfile", str(signature_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3, check=False,
            )
        return result.returncode == 0
    except (ValueError, OSError, subprocess.SubprocessError):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "MoodleSafeExecutor/1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Never emit bodies, approvals, signatures, or query strings.
        print(f"safe-executor {self.client_address[0]} {self.command} {self.path.split('?')[0]}", flush=True)

    def respond(self, status: int, payload: dict[str, Any]) -> None:
        raw = canonical(payload)
        signed = b"RESPONSE\n" + str(status).encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
        signature = hmac.new(AGENT_KEY, signed, hashlib.sha256).hexdigest()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Executor-Response-Signature", signature)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self.respond(404, {"status": "not_found"})
            return
        self.respond(200, {
            "status": "healthy",
            "execution_enabled": os.environ.get("EXECUTOR_LIVE_ENABLED", "false").lower() == "true",
        })

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/execute":
            self.respond(404, {"status": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                self.respond(413, {"status": "denied", "reason": "invalid_body_size"})
                return
            raw = self.rfile.read(length)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self.respond(400, {"status": "denied", "reason": "invalid_json"})
            return

        timestamp = self.headers.get("X-Executor-Timestamp", "")
        nonce = self.headers.get("X-Executor-Nonce", "")
        signature = self.headers.get("X-Executor-Signature", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", nonce) or not HEX_64.fullmatch(signature):
            self.respond(401, {"status": "denied", "reason": "invalid_request_auth"})
            return
        try:
            timestamp_value = int(timestamp)
        except ValueError:
            timestamp_value = 0
        if abs(int(time.time()) - timestamp_value) > 30:
            self.respond(401, {"status": "denied", "reason": "invalid_request_auth"})
            return
        signed = b"POST\n/v1/execute\n" + timestamp.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
        expected = hmac.new(AGENT_KEY, signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            self.respond(401, {"status": "denied", "reason": "invalid_request_auth"})
            return

        required = {"scenario_id", "catalog_action_id", "target_scope", "environment", "action_sha256", "idempotency_key", "approval"}
        if set(body) != required or body.get("environment") != "staging":
            self.respond(403, {"status": "denied", "reason": "request_schema_or_environment_rejected"})
            return
        scenario, action_id, scope = body["scenario_id"], body["catalog_action_id"], body["target_scope"]
        key, supplied_digest = body["idempotency_key"], body["action_sha256"]
        if not all(isinstance(value, str) for value in (scenario, action_id, scope, key, supplied_digest)):
            self.respond(400, {"status": "denied", "reason": "invalid_typed_request"})
            return
        nodes = CATALOG.get((scenario, action_id, scope))
        if nodes is None:
            self.respond(403, {"status": "denied", "reason": "outside_executor_allowlist_or_action_hash_mismatch"})
            return
        if not IDEMPOTENCY.fullmatch(key):
            self.respond(400, {"status": "denied", "reason": "invalid_idempotency_key"})
            return
        expected_digest = action_digest(scenario, action_id, scope, key)
        if supplied_digest != expected_digest:
            self.respond(403, {"status": "denied", "reason": "outside_executor_allowlist_or_action_hash_mismatch"})
            return
        if not valid_approval(body["approval"], expected_digest):
            self.respond(403, {"status": "awaiting_approval", "reason": "approval_missing_invalid_expired_or_wrong_action"})
            return
        if os.environ.get("EXECUTOR_LIVE_ENABLED", "false").lower() != "true":
            self.respond(423, {"status": "disabled", "reason": "live_execution_kill_switch_off"})
            return

        request_hash = hashlib.sha256(canonical({k: v for k, v in body.items() if k != "approval"})).hexdigest()
        # Atomically claim the idempotency key and nonce before any mutation.
        with sqlite3.connect(STATE_DB, timeout=5, isolation_level="IMMEDIATE") as db:
            db.execute("DELETE FROM request_nonces WHERE expires_at < ?", (int(time.time()),))
            try:
                db.execute("INSERT INTO request_nonces VALUES (?, ?)", (nonce, int(time.time()) + 60))
            except sqlite3.IntegrityError:
                self.respond(409, {"status": "denied", "reason": "replayed_request_nonce"})
                return
            previous = db.execute("SELECT body_sha256, status, result_json FROM executions WHERE idempotency_key = ?", (key,)).fetchone()
            if previous:
                if previous[0] != request_hash:
                    self.respond(409, {"status": "denied", "reason": "idempotency_key_conflict"})
                elif previous[1] == "running":
                    self.respond(409, {"status": "in_progress", "reason": "idempotent_request_in_progress"})
                else:
                    self.respond(200, json.loads(previous[2]))
                return
            db.execute("INSERT INTO executions VALUES (?, ?, 'running', NULL, ?)", (key, request_hash, datetime.now(timezone.utc).isoformat()))

        completed: list[str] = []
        failure: dict[str, Any] | None = None
        for node in nodes:
            command = [
                "ssh", "-F", "/dev/null", "-i", str(SSH_KEY),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "ConnectTimeout=8",
                f"aiops-executor@{NODES[node]}", scenario,
            ]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
            except subprocess.TimeoutExpired:
                failure = {"node": node, "status": "timeout"}
                break
            if result.returncode != 0:
                failure = {"node": node, "status": "failed", "exit_code": result.returncode}
                break
            completed.append(node)

        response = {
            "status": "executed" if failure is None else "partial_failure" if completed else "failed",
            "scenario_id": scenario,
            "completed_nodes": completed,
            "failure": failure,
            "mutated": bool(completed),
        }
        with sqlite3.connect(STATE_DB) as db:
            db.execute("UPDATE executions SET status = ?, result_json = ? WHERE idempotency_key = ?", (response["status"], canonical(response).decode(), key))
        self.respond(200 if failure is None else 502, response)


if __name__ == "__main__":
    if len(AGENT_KEY) < 32 or not APPROVAL_PUBLIC_KEY.is_file():
        raise SystemExit("executor transport key or approval public key is missing/invalid")
    init_db()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
