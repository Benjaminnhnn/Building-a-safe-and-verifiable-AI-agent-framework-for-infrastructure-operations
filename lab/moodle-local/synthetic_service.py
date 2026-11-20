#!/usr/bin/env python3
"""Runs the unmodified staging synthetic-transaction probe against the lab LB and serves its .prom file for Prometheus."""
from __future__ import annotations

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TEXTFILES = Path("/var/lib/moodle-synthetic/textfiles")
PASSWORD_FILE = Path("/run/lab-admin-password")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"".join(p.read_bytes() for p in sorted(TEXTFILES.glob("*.prom")))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


if __name__ == "__main__":
    TEXTFILES.mkdir(parents=True, exist_ok=True)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(os.environ["LAB_ADMIN_PASSWORD"])
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 9102), Handler).serve_forever, daemon=True).start()
    raise SystemExit(subprocess.call([
        "python", "/app/automation/moodle-synthetic-transaction.py",
        "--url", "http://moodle-lab:8080", "--password-file", str(PASSWORD_FILE),
        "--output-dir", "/var/lib/moodle-synthetic/state", "--metrics-file", str(TEXTFILES / "moodle_synthetic.prom"),
        "--duration-seconds", "31536000", "--interval-seconds", os.environ.get("LAB_SYNTHETIC_INTERVAL", "10"),
    ]))
