"""Local-only synthetic Moodle LDAP login metrics. Never logs credentials."""
from __future__ import annotations

import http.cookiejar
import hmac
import json
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


def secret(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        value = handle.read().strip()
    if not value:
        raise RuntimeError("synthetic probe secret file is empty")
    return value


class TokenParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            fields = dict(attrs)
            if fields.get("name") == "logintoken":
                self.token = fields.get("value")


def login_outcome(username: str, password: str) -> str:
    url = os.environ["MOODLE_LOGIN_URL"]
    cookies = http.cookiejar.CookieJar()
    canonical_host = os.environ["MOODLE_CANONICAL_HOST"]

    class CanonicalRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            destination = urlparse(newurl)
            if destination.hostname in {"127.0.0.1", "localhost"}:
                from urllib.parse import urlunparse
                current = urlparse(req.full_url)
                newurl = urlunparse(destination._replace(netloc=current.netloc))
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            if redirected is not None:
                redirected.add_unredirected_header("Host", canonical_host)
            return redirected

    client = build_opener(HTTPCookieProcessor(cookies), CanonicalRedirect())
    try:
        page = Request(url, headers={"Host": canonical_host})
        with client.open(page, timeout=8) as response:
            parser = TokenParser()
            parser.feed(response.read().decode("utf-8", errors="replace"))
            if not parser.token:
                return "error"
        body = urlencode({"username": username, "password": password, "logintoken": parser.token}).encode()
        req = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Host": canonical_host})
        with client.open(req, timeout=8) as response:
            final = urlparse(response.geturl())
            same_origin = urlparse(url).netloc == final.netloc
            on_login_form = final.path.rstrip("/").endswith("/login/index.php")
            has_session = any(cookie.name == "MoodleSession" for cookie in cookies)
            if not same_origin:
                return "error"
            if on_login_form:
                return "denied"
            return "authenticated" if has_session else "error"
    except (HTTPError, URLError, TimeoutError, OSError):
        return "error"


def login(username: str, password: str) -> bool:
    return login_outcome(username, password) == "authenticated"


def endpoint_reachable() -> bool:
    try:
        request = Request(
            os.environ["MOODLE_HEALTH_URL"],
            headers={"Host": os.environ["MOODLE_CANONICAL_HOST"]},
        )
        with build_opener().open(request, timeout=5) as response:
            return response.status == 200
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def ldaps_certificate_valid() -> bool:
    try:
        context = ssl.create_default_context(cafile=os.environ["LDAP_HEALTH_CA_FILE"])
        with socket.create_connection((os.environ["LDAP_HEALTH_HOST"], 636), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=os.environ["LDAP_HEALTH_HOST"]):
                return True
    except (OSError, ssl.SSLError):
        return False


def verify_recovery() -> dict:
    """Independent 120-second login, denial, health, and stability check."""
    username = secret(os.environ["AUTH_USER_FILE"])
    password = secret(os.environ["AUTH_PASSWORD_FILE"])
    valid_passed = True
    invalid_denied = True
    health_passed = True
    ldaps_passed = True
    observations = 0
    stability_observations = []
    first_observation_monotonic = None
    while True:
        valid_sample_passed = login_outcome(username, password) == "authenticated"
        invalid_sample_denied = login_outcome(
            "aiops-invalid-probe-user", "invalid-probe-no-secret"
        ) == "denied"
        valid_passed &= valid_sample_passed
        invalid_denied &= invalid_sample_denied
        healthy = endpoint_reachable()
        ldaps_healthy = ldaps_certificate_valid()
        health_passed &= healthy
        ldaps_passed &= ldaps_healthy
        observed_at = datetime.now(timezone.utc)
        observed_monotonic = time.monotonic()
        if first_observation_monotonic is None:
            first_observation_monotonic = observed_monotonic
        stability_observations.append({
            "observed_at": observed_at.isoformat(),
            "healthy": healthy and ldaps_healthy and valid_sample_passed and invalid_sample_denied,
            "simulated": False,
        })
        observations += 1
        if observed_monotonic - first_observation_monotonic >= 120:
            break
        time.sleep(15)
    stability_seconds = int(
        (
            datetime.fromisoformat(stability_observations[-1]["observed_at"])
            - datetime.fromisoformat(stability_observations[0]["observed_at"])
        ).total_seconds()
    )
    return {
        "authority": "independent_verifier",
        "simulated": False,
        "valid_login_passed": valid_passed,
        "invalid_login_denied": invalid_denied,
        "health_passed": health_passed,
        "ldaps_passed": ldaps_passed,
        "stability_seconds": stability_seconds,
        "observations": observations,
        "stability_observations": stability_observations,
        "related_probes": [
            {
                "name": "synthetic-valid-login-passes",
                "passed": valid_passed,
                "simulated": False,
                "details": "Independent synthetic valid-account login probe",
            },
            {
                "name": "synthetic-invalid-login-is-denied",
                "passed": invalid_denied,
                "simulated": False,
                "details": "Independent synthetic invalid-account denial probe",
            },
            {
                "name": "moodle-health-passes",
                "passed": health_passed,
                "simulated": False,
                "details": "Independent Moodle health endpoint probe",
            },
        ],
        "resolution_eligible": valid_passed and invalid_denied and health_passed and ldaps_passed,
    }


state = {"valid_login_success": 0, "moodle_login_endpoint_reachable": 0, "probe_outcome_code": 0}


def sample() -> None:
    username = secret(os.environ["AUTH_USER_FILE"])
    password = secret(os.environ["AUTH_PASSWORD_FILE"])
    while True:
        state["valid_login_success"] = int(login(username, password))
        state["probe_outcome_code"] = 1 if state["valid_login_success"] else 2
        state["moodle_login_endpoint_reachable"] = int(endpoint_reachable())
        time.sleep(max(5, int(os.environ.get("AUTH_PROBE_INTERVAL", "15"))))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = b'{"status":"healthy"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = "".join(
            f"# TYPE auth_lab_{name} gauge\nauth_lab_{name} {value}\n"
            for name, value in state.items()
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if os.getenv("AUTH_VERIFIER_MODE") != "true" or self.path != "/verify" or not hmac.compare_digest(
            self.headers.get("Authorization", ""),
            "Bearer " + os.environ["AUTH_LAB_RECOVERY_TOKEN"],
        ):
            self.send_error(404)
            return
        if int(self.headers.get("Content-Length", "0")) != 0:
            self.send_error(400)
            return
        result = json.dumps(verify_recovery(), sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, fmt, *args):
        return


if os.getenv("AUTH_VERIFIER_MODE") != "true":
    threading.Thread(target=sample, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", int(os.getenv("AUTH_SERVICE_PORT", "9105"))), Handler).serve_forever()
