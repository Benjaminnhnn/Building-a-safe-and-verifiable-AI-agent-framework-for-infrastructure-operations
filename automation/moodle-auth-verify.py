"""Run the independent, read-only Moodle synthetic-login contract probe.

Credentials are read from operator-owned files and never written to evidence.
The probe does not change incident state or remediate infrastructure.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from core.live_adapters import MoodleAuthVerificationAdapter


def main() -> int:
    required = ("MOODLE_AUTH_PROBE_BASE_URL", "MOODLE_AUTH_TEST_USER_FILE", "MOODLE_AUTH_TEST_PASSWORD_FILE")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("Missing required verifier configuration: " + ", ".join(missing), file=sys.stderr)
        return 64

    adapter = MoodleAuthVerificationAdapter(
        os.environ["MOODLE_AUTH_PROBE_BASE_URL"],
        os.environ["MOODLE_AUTH_TEST_USER_FILE"],
        os.environ["MOODLE_AUTH_TEST_PASSWORD_FILE"],
        ca_bundle=os.environ.get("MOODLE_AUTH_CA_BUNDLE"),
        allow_local_http=os.environ.get("MOODLE_AUTH_ALLOW_LOCAL_HTTP") == "true",
    )

    def report_observation(observation: dict) -> None:
        print(json.dumps(observation, sort_keys=True), flush=True)

    try:
        result = adapter.verify(on_observation=report_observation)
    except Exception:
        # Exceptions can contain URLs, local paths or account details. Keep the
        # terminal output deliberately generic; diagnose from protected local logs.
        print("Verifier could not complete; no credentials or raw exception were emitted.", file=sys.stderr)
        return 2
    result.pop("observations", None)
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["resolution_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
