#!/usr/bin/env python3
"""Send an explicitly confirmed, non-remediation probe to a loopback AI API."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests


def loopback_webhook(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.path == "/webhook"
        and parsed.username is None
        and parsed.password is None
    )


def build_payload() -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "ManualValidationProbe",
                "severity": "info",
                "service": "manual-validation",
                "environment": "local",
            },
            "annotations": {
                "summary": "Operator-requested webhook connectivity probe",
                "description": "Synthetic validation event; no infrastructure fault is asserted.",
            },
            "startsAt": now,
            "generatorURL": "http://localhost/manual-validation",
        }],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhook")
    parser.add_argument("--send", action="store_true", help="actually POST the synthetic event")
    args = parser.parse_args()
    if not loopback_webhook(args.url):
        parser.error("only an HTTP /webhook endpoint on localhost/127.0.0.1/::1 is allowed")
    payload = build_payload()
    if not args.send:
        print(json.dumps(payload, indent=2))
        print("dry-run only; add --send to POST this synthetic info-level event to loopback")
        return 0
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(args.url, json=payload, timeout=5, allow_redirects=False)
    except requests.RequestException as exc:
        print(f"webhook request failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"HTTP {response.status_code}; response body omitted")
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
