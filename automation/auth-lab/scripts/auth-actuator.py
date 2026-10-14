"""Loopback-lab actuator exposing one Docker action for one Compose service.

This container owns the Docker socket; the AI agent and Celery worker do not.
The only accepted operation is restart/start of this project's OpenLDAP
container, followed by its Docker health check. This is a lab trust boundary,
not a production-grade sandbox for an untrusted/compromised actuator.
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

SOCKET_PATH = "/var/run/docker.sock"
PROJECT = "moodle-auth-lab"
SERVICE = "openldap"
TOKEN = os.environ["AUTH_LAB_RECOVERY_TOKEN"]
_lock = threading.Lock()
_last_action_at = 0.0


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET_PATH)


def docker_request(method: str, path: str) -> tuple[int, object]:
    connection = UnixHTTPConnection("localhost", timeout=5)
    connection.request(method, path, headers={"Host": "docker"})
    response = connection.getresponse()
    body = response.read()
    status = response.status
    connection.close()
    if not body:
        return status, None
    try:
        return status, json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return status, None


def locate_target() -> dict:
    filters = json.dumps({"label": [
        f"com.docker.compose.project={PROJECT}",
        f"com.docker.compose.service={SERVICE}",
    ]}, separators=(",", ":"))
    status, containers = docker_request(
        "GET", "/containers/json?all=1&" + urlencode({"filters": filters})
    )
    if status != 200 or not isinstance(containers, list) or len(containers) != 1:
        raise RuntimeError("Expected exactly one OpenLDAP service container")
    target = containers[0]
    labels = target.get("Labels") or {}
    if labels.get("com.docker.compose.project") != PROJECT or labels.get(
        "com.docker.compose.service"
    ) != SERVICE:
        raise RuntimeError("Docker returned a container outside the fixed service scope")
    return target


def recover_openldap() -> dict:
    global _last_action_at
    with _lock:
        now = time.monotonic()
        if now - _last_action_at < 180:
            raise RuntimeError("Local recovery cooldown is active")
        _last_action_at = now

        target = locate_target()
        container_id = target.get("Id")
        state = target.get("State")
        if not container_id or state not in {"running", "exited"}:
            raise RuntimeError("OpenLDAP is not in a restartable container state")

        operation = "restart" if state == "running" else "start"
        endpoint = f"/containers/{container_id}/{operation}"
        if operation == "restart":
            endpoint += "?t=10"
        status, _ = docker_request("POST", endpoint)
        if status not in {204, 304}:
            raise RuntimeError(f"Docker rejected the fixed OpenLDAP {operation} request")

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            inspect_status, inspected = docker_request(
                "GET", f"/containers/{container_id}/json"
            )
            health = None
            if inspect_status == 200 and isinstance(inspected, dict):
                health = (inspected.get("State", {}).get("Health") or {}).get("Status")
            if health == "healthy":
                return {"target": "local-openldap", "operation": operation, "health": health}
            if health == "unhealthy":
                raise RuntimeError("OpenLDAP reported unhealthy after the fixed action")
            time.sleep(2)
        raise RuntimeError("OpenLDAP did not become healthy before the 60-second deadline")


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "healthy", "scope": "local-openldap-only"})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/v1/auth-01/openldap/restart":
            self._json(404, {"error": "not_found"})
            return
        expected = "Bearer " + TOKEN
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            self._json(403, {"error": "forbidden"})
            return
        if int(self.headers.get("Content-Length", "0")) != 0:
            self._json(400, {"error": "request_body_not_allowed"})
            return
        try:
            self._json(200, {"status": "succeeded", **recover_openldap()})
        except Exception as error:  # Keep Docker internals and paths out of response.
            self._json(409, {"status": "denied_or_failed", "reason": str(error)[:180]})

    def log_message(self, fmt: str, *args) -> None:
        # Do not log auth headers, request bodies, or arbitrary caller strings.
        return


ThreadingHTTPServer(("0.0.0.0", 9191), Handler).serve_forever()
