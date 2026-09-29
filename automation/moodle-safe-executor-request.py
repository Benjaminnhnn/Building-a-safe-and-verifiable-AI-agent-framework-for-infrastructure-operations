#!/usr/bin/env python3
"""Send an already signed approval to the private staging Executor API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / "agent_src"))
from core.safe_executor_client import execute_approved_staging_action


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("approval_file", type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--hmac-key-file", required=True, type=Path)
    parser.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    request = json.loads(args.approval_file.read_text(encoding="utf-8"))
    if request.get("idempotency_key") != args.idempotency_key:
        parser.error("idempotency key differs from the action approval signature")
    os.environ["SAFE_EXECUTOR_URL"] = args.endpoint
    os.environ["SAFE_EXECUTOR_HMAC_KEY"] = args.hmac_key_file.read_text(encoding="ascii").strip()
    result = execute_approved_staging_action(
        scenario_id=request["scenario_id"],
        catalog_action_id=request["catalog_action_id"],
        target_scope=request["target_scope"],
        idempotency_key=args.idempotency_key,
        approval=request["approval"],
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "executed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
