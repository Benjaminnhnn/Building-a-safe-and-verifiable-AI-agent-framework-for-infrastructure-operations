#!/usr/bin/env python3
"""Read-only authenticated ERPNext probe; token is read from a mode-0600 file."""

from __future__ import annotations

import argparse
import json
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="ERPNext site URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    token_mode = stat.S_IMODE(args.token_file.stat().st_mode)
    if token_mode & 0o077:
        print("token file must not be accessible by group or other users", file=sys.stderr)
        return 2
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token or "\n" in token:
        print("invalid token file", file=sys.stderr)
        return 2
    url = args.base_url.rstrip("/") + "/api/method/frappe.auth.get_logged_user"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"token {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            payload = json.load(response)
            user = payload.get("message") if isinstance(payload, dict) else None
            if response.status != 200 or not isinstance(user, str) or not user or user == "Guest":
                print("authenticated transaction failed", file=sys.stderr)
                return 1
            print(json.dumps({"status": "ok", "authenticated": True, "user": user}))
            return 0
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"ERPNext synthetic transaction failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
