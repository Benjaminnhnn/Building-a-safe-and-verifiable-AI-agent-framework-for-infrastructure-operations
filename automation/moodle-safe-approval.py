#!/usr/bin/env python3
"""Create an action-bound Ed25519 approval without exposing the private key."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATALOG = {
    "DB-01": ("remove_scoped_db_reject", "staging_moodle_nodes"),
    "RES-01": ("remove_named_cpu_load_container", "moodle-app-b"),
    "NET-01": ("recreate_moodle_web_from_reviewed_compose", "staging_moodle_nodes"),
    "CON-01": ("start_reviewed_compose_service", "moodle-app-b"),
    "SEC-02": ("restore_fixture_directory_mode", "moodledata_synthetic_fixture_directory"),
}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=sorted(CATALOG))
    parser.add_argument("--private-key", required=True, type=Path, help="Ed25519 private key held by the approving operator")
    parser.add_argument("--idempotency-key", required=True, help="Unique execution key signed for exactly one request")
    parser.add_argument("--actor", required=True, choices=("operator", "human-approver"))
    parser.add_argument("--ttl-minutes", type=int, default=5, choices=range(1, 16))
    args = parser.parse_args()

    action_id, scope = CATALOG[args.scenario]
    digest = hashlib.sha256(canonical({
        "environment": "staging",
        "scenario_id": args.scenario,
        "catalog_action_id": action_id,
        "target_scope": scope,
        "idempotency_key": args.idempotency_key,
    })).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=args.ttl_minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
    message = f"{digest}:{args.actor}:{expires_at}".encode()
    with tempfile.TemporaryDirectory(prefix="moodle-approval-sign-") as directory:
        message_path = Path(directory) / "approval.txt"
        message_path.write_bytes(message)
        signed = subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(args.private_key), "-rawin", "-in", str(message_path)],
            capture_output=True, check=True,
        ).stdout
    print(json.dumps({
        "scenario_id": args.scenario,
        "catalog_action_id": action_id,
        "target_scope": scope,
        "environment": "staging",
        "action_sha256": digest,
        "idempotency_key": args.idempotency_key,
        "approval": {
            "actor_id": args.actor,
            "expires_at": expires_at,
            "action_sha256": digest,
            "signature": base64.b64encode(signed).decode("ascii"),
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
