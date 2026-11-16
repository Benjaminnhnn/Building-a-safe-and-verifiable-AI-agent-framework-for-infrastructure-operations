"""Lab executor: the staging safe-executor catalog semantics against the local Docker lab.

This is a lab-only port of automation/moodle-safe-executor-api.py (it never talks to AWS or the staging executor and
does not modify it). Preserved semantics: exact (scenario, action, scope) allowlist, request schema, action digest
bound to scenario/action/scope/idempotency key, signed expiring approval for that exact digest, atomic nonce and
idempotency claims before any mutation, per-node sequential execution, and the same response statuses
(`executed`, `partial_failure`, `failed`, `denied`, `awaiting_approval`, `disabled`). Differences: environment is
`lab`, the "remote nodes" are Docker containers of the local project, and approval is signed by an automated lab key
(there is no human Operator in the lab, so lab approvals are never staging evidence).
"""
from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import lab_faults
from action_ladder import ACTION_SPECS
from lab_env import LabCommandError, canonical_json

ENVIRONMENT = "lab"
IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
NONCE = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
REQUIRED = {"scenario_id", "catalog_action_id", "target_scope", "environment", "action_sha256", "idempotency_key", "approval"}

# Exact catalog (same keys as CATALOG in the staging API, restricted to the six reviewed live actions).
CATALOG = {(s.scenario, s.action_id, s.scope): s.nodes for s in ACTION_SPECS.values() if s.scenario}

# What each catalog action does on one node. Mirrors the forced-command script's allowlist.
NODE_ACTIONS = {
    "remove_scoped_db_reject": lambda node: lab_faults.repair_remove_scoped_db_reject(),
    "recreate_moodle_web_from_reviewed_compose": lab_faults.repair_recreate_web,
    "remove_named_cpu_load_container": lambda node: lab_faults.repair_remove_cpu_load(),
    "start_reviewed_compose_service": lab_faults.repair_start_web,
    "restore_fixture_directory_mode": lambda node: lab_faults.repair_restore_fixture_mode(),
    "restart_moodle_cron_service": lambda node: lab_faults.repair_start_cron(),
}


def action_digest(scenario: str, action_id: str, scope: str, idempotency_key: str) -> str:
    return hashlib.sha256(canonical_json({
        "environment": ENVIRONMENT, "scenario_id": scenario, "catalog_action_id": action_id,
        "target_scope": scope, "idempotency_key": idempotency_key,
    })).hexdigest()


class LabApprover:
    """Automated lab signer. Holds an Ed25519 key; public half is given to the executor."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key

    @classmethod
    def load_or_create(cls, path: Path) -> "LabApprover":
        if path.exists():
            return cls(serialization.load_pem_private_key(path.read_bytes(), password=None))
        key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return cls(key)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def approve(self, digest: str, actor: str = "operator", ttl_seconds: int = 300) -> dict:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        signature = self._key.sign(f"{digest}:{actor}:{expires}".encode())
        return {"actor_id": actor, "expires_at": expires, "action_sha256": digest,
                "signature": base64.b64encode(signature).decode()}


def valid_approval(approval: object, digest: str, public_key: Ed25519PublicKey) -> bool:
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
        now = datetime.now(timezone.utc)
        if expiry <= now or expiry > now + timedelta(minutes=15):
            return False
        public_key.verify(base64.b64decode(signature, validate=True), f"{digest}:{actor}:{expires_at}".encode())
        return True
    except Exception:
        return False


class LabExecutor:
    def __init__(self, state_db: Path, public_key: Ed25519PublicKey, *, live_enabled: bool = True):
        self.state_db = state_db
        self.public_key = public_key
        self.live_enabled = live_enabled
        state_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state_db) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS executions (
                idempotency_key TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL,
                status TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS request_nonces (nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")

    def execute(self, body: dict, nonce: str) -> tuple[int, dict]:
        """Fail closed on every validation step; mutate only after both claims succeed."""
        import json
        if not isinstance(body, dict) or set(body) != REQUIRED or body.get("environment") != ENVIRONMENT:
            return 403, {"status": "denied", "reason": "request_schema_or_environment_rejected"}
        scenario, action_id, scope = body["scenario_id"], body["catalog_action_id"], body["target_scope"]
        key, supplied = body["idempotency_key"], body["action_sha256"]
        if not all(isinstance(v, str) for v in (scenario, action_id, scope, key, supplied)):
            return 400, {"status": "denied", "reason": "invalid_typed_request"}
        nodes = CATALOG.get((scenario, action_id, scope))
        if nodes is None:
            return 403, {"status": "denied", "reason": "outside_executor_allowlist_or_action_hash_mismatch"}
        if not IDEMPOTENCY.fullmatch(key) or not NONCE.fullmatch(nonce or ""):
            return 400, {"status": "denied", "reason": "invalid_idempotency_key"}
        expected = action_digest(scenario, action_id, scope, key)
        if supplied != expected:
            return 403, {"status": "denied", "reason": "outside_executor_allowlist_or_action_hash_mismatch"}
        if not valid_approval(body["approval"], expected, self.public_key):
            return 403, {"status": "awaiting_approval", "reason": "approval_missing_invalid_expired_or_wrong_action"}
        if not self.live_enabled:
            return 423, {"status": "disabled", "reason": "live_execution_kill_switch_off", "mutated": False, "completed_nodes": []}
        request_hash = hashlib.sha256(canonical_json({k: v for k, v in body.items() if k != "approval"})).hexdigest()
        with sqlite3.connect(self.state_db, timeout=5, isolation_level="IMMEDIATE") as db:
            db.execute("DELETE FROM request_nonces WHERE expires_at < ?", (int(time.time()),))
            try:
                db.execute("INSERT INTO request_nonces VALUES (?, ?)", (nonce, int(time.time()) + 60))
            except sqlite3.IntegrityError:
                return 409, {"status": "denied", "reason": "replayed_request_nonce"}
            previous = db.execute("SELECT body_sha256, status, result_json FROM executions WHERE idempotency_key = ?", (key,)).fetchone()
            if previous:
                if previous[0] != request_hash:
                    return 409, {"status": "denied", "reason": "idempotency_key_conflict"}
                if previous[1] == "running":
                    return 409, {"status": "in_progress", "reason": "idempotent_request_in_progress"}
                return 200, json.loads(previous[2])
            db.execute("INSERT INTO executions VALUES (?, ?, 'running', NULL, ?)", (key, request_hash, datetime.now(timezone.utc).isoformat()))

        completed: list[str] = []
        failure = None
        for node in nodes:
            try:
                NODE_ACTIONS[action_id](node)
            except LabCommandError as exc:
                failure = {"node": node, "status": "timeout" if "timeout" in str(exc) else "failed"}
                break
            completed.append(node)
        response = {
            "status": "executed" if failure is None else "partial_failure" if completed else "failed",
            "scenario_id": scenario, "completed_nodes": completed, "failure": failure, "mutated": bool(completed),
        }
        if failure is not None and not completed:
            response["mutated"] = None
            response["mutation_state"] = "unknown"
        with sqlite3.connect(self.state_db) as db:
            db.execute("UPDATE executions SET status = ?, result_json = ? WHERE idempotency_key = ?",
                       (response["status"], canonical_json(response).decode(), key))
        return (200 if failure is None else 502), response


def build_request(action_id: str, idempotency_key: str, approver: LabApprover | None) -> dict:
    """Build the exact request body for a catalog action (scenario/scope come from the single-source ladder)."""
    s = ACTION_SPECS[action_id]
    if not s.scenario:
        raise ValueError(f"{action_id} is not an executor catalog action")
    digest = action_digest(s.scenario, action_id, s.scope, idempotency_key)
    return {"scenario_id": s.scenario, "catalog_action_id": action_id, "target_scope": s.scope, "environment": ENVIRONMENT,
            "action_sha256": digest, "idempotency_key": idempotency_key,
            "approval": approver.approve(digest) if approver else {}}
