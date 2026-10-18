#!/usr/bin/env python3
"""Create an action-bound Safety Gate approval using an operator-held key."""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action-sha256", required=True)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--actor", required=True, choices=("safety-officer",))
    parser.add_argument("--ttl-minutes", type=int, default=5, choices=range(1, 16))
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.action_sha256):
        parser.error("action digest must be a lowercase SHA-256 hex value")

    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=args.ttl_minutes)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    message = f"{args.action_sha256}:{args.actor}:{expires_at}".encode()
    with tempfile.TemporaryDirectory(prefix="moodle-gate-approval-") as directory:
        message_path = Path(directory) / "approval.txt"
        message_path.write_bytes(message)
        signed = subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(args.private_key), "-rawin", "-in", str(message_path)],
            capture_output=True,
            check=True,
        ).stdout
    signature = base64.b64encode(signed).decode("ascii")
    print(
        '{"action_sha256":"%s","actor_id":"%s","expires_at":"%s",'
        '"signature":"ed25519:%s"}'
        % (args.action_sha256, args.actor, expires_at, signature)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
