"""Read-only synthetic authentication probes for an independently run verifier.

This module is deliberately not imported by the alert-processing agent. Keep
test-account credentials in operator-managed files and only pass the boolean
probe verdicts to the agent/audit pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests


@dataclass(frozen=True)
class LoginObservation:
    authenticated: bool
    elapsed_ms: float
    outcome: str


class _LoginTokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        fields = dict(attrs)
        if fields.get("name") == "logintoken":
            self.token = fields.get("value")


class MoodleLoginProbe:
    """Perform one Moodle form login without retaining or logging credentials."""

    simulated = False

    def __init__(
        self,
        base_url: str,
        *,
        verify_tls: bool | str = True,
        allow_local_http: bool = False,
        timeout_seconds: float = 10.0,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        if verify_tls is False:
            raise ValueError("TLS certificate verification cannot be disabled")
        parsed = urlparse(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (allow_local_http and local_http):
            raise ValueError("Moodle login probe requires HTTPS; HTTP is limited to explicit loopback labs")
        if not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Moodle base URL must be an origin/path without embedded credentials")
        self.base_url = base_url.rstrip("/") + "/"
        self.verify_tls = verify_tls
        self.timeout_seconds = timeout_seconds
        self.session_factory = session_factory

    def _same_origin(self, url: str) -> bool:
        destination = urlparse(url)
        origin = urlparse(self.base_url)
        if destination.username or destination.password:
            return False
        try:
            destination_port = destination.port or (
                443 if destination.scheme == "https" else 80
            )
            origin_port = origin.port or (443 if origin.scheme == "https" else 80)
        except ValueError:
            return False
        return (
            destination.scheme == origin.scheme
            and destination.hostname == origin.hostname
            and destination_port == origin_port
        )

    def login(self, username: str, password: str) -> LoginObservation:
        started = time.monotonic()
        session = self.session_factory()
        login_url = urljoin(self.base_url, "login/index.php")
        try:
            page = session.get(
                login_url,
                timeout=self.timeout_seconds,
                verify=self.verify_tls,
                allow_redirects=False,
            )
            page.raise_for_status()
            if not self._same_origin(page.url) or 300 <= page.status_code < 400:
                return LoginObservation(False, (time.monotonic() - started) * 1000, "error")
            parser = _LoginTokenParser()
            parser.feed(page.text)
            if not parser.token:
                return LoginObservation(False, (time.monotonic() - started) * 1000, "error")
            response = session.post(
                login_url,
                data={"username": username, "password": password, "logintoken": parser.token},
                timeout=self.timeout_seconds,
                verify=self.verify_tls,
                allow_redirects=False,
            )
            response.raise_for_status()
            final_url = response.url
            redirects = 0
            while 300 <= response.status_code < 400 and redirects < 5:
                location = response.headers.get("Location", "")
                final_url = urljoin(login_url, location)
                if not location or not self._same_origin(final_url):
                    return LoginObservation(False, (time.monotonic() - started) * 1000, "error")
                response = session.get(
                    final_url,
                    timeout=self.timeout_seconds,
                    verify=self.verify_tls,
                    allow_redirects=False,
                )
                response.raise_for_status()
                final_url = response.url
                redirects += 1
            if 300 <= response.status_code < 400:
                return LoginObservation(False, (time.monotonic() - started) * 1000, "error")
            final = urlparse(final_url)
            authenticated = (
                self._same_origin(final_url)
                and not final.path.rstrip("/").endswith("/login/index.php")
                and session.cookies.get("MoodleSession") is not None
            )
            outcome = "authenticated" if authenticated else (
                "denied" if final.path.rstrip("/").endswith("/login/index.php") else "error"
            )
            return LoginObservation(authenticated, (time.monotonic() - started) * 1000, outcome)
        except requests.RequestException:
            # Never serialize the exception: it may contain request details.
            return LoginObservation(False, (time.monotonic() - started) * 1000, "error")
        finally:
            session.close()


class MoodleAuthVerifier:
    """Verify valid-user login, invalid-user denial, health and stability.

    ``verify`` returns only non-identifying booleans/timestamps. Callers must
    keep this verifier separate from the agent/executor and guard the
    RESOLVED transition through the repository's independent-verifier state
    authority.
    """

    def __init__(
        self,
        login_probe: MoodleLoginProbe,
        health_probe: Callable[[], bool],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.login_probe = login_probe
        self.health_probe = health_probe
        self.simulated = not (
            getattr(login_probe, "simulated", True) is False
            and getattr(health_probe, "simulated", True) is False
        )
        self.monotonic = monotonic
        self.sleep = sleep

    def verify(
        self,
        *,
        valid_username: str,
        valid_password: str,
        denied_username: str,
        denied_password: str,
        stability_seconds: int = 120,
        sample_interval_seconds: int = 15,
        on_observation: Callable[[dict], None] | None = None,
    ) -> dict:
        if (
            isinstance(stability_seconds, bool)
            or not isinstance(stability_seconds, int)
            or stability_seconds < 120
        ):
            raise ValueError("authentication recovery requires at least 120 seconds of stability")
        if (
            isinstance(sample_interval_seconds, bool)
            or not isinstance(sample_interval_seconds, int)
            or sample_interval_seconds < 1
        ):
            raise ValueError("sample interval must be positive")

        start = self.monotonic()
        observations: list[dict] = []
        valid_login_passed = True
        invalid_login_denied = True
        health_passed = True
        first_sample = True
        while True:
            elapsed_before = self.monotonic() - start
            final_sample = elapsed_before >= stability_seconds
            check_login = first_sample or final_sample
            valid = denied = None
            if check_login:
                try:
                    valid = self.login_probe.login(valid_username, valid_password)
                except Exception:
                    valid = None
                try:
                    denied = self.login_probe.login(denied_username, denied_password)
                except Exception:
                    denied = None
                valid = (
                    valid
                    if isinstance(valid, LoginObservation)
                    and type(valid.authenticated) is bool
                    and valid.outcome in {"authenticated", "denied", "error"}
                    else None
                )
                denied = (
                    denied
                    if isinstance(denied, LoginObservation)
                    and type(denied.authenticated) is bool
                    and denied.outcome in {"authenticated", "denied", "error"}
                    else None
                )
            try:
                healthy = self.health_probe() is True
            except Exception:
                healthy = False
            if check_login:
                valid_login_passed &= bool(
                    valid
                    and valid.authenticated is True
                    and valid.outcome == "authenticated"
                )
                invalid_login_denied &= bool(
                    denied
                    and denied.authenticated is False
                    and denied.outcome == "denied"
                )
            health_passed &= healthy
            observations.append({
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "valid_login": valid.authenticated if valid else None,
                "invalid_login_denied": denied.outcome == "denied" if denied else None,
                "health": healthy,
                "simulated": self.simulated,
            })
            if on_observation is not None:
                on_observation(observations[-1].copy())
            elapsed = self.monotonic() - start
            if elapsed >= stability_seconds:
                if not check_login:
                    # A slow health request may cross the deadline. Take the
                    # final identity probes before leaving the stability gate.
                    continue
                break
            self.sleep(min(sample_interval_seconds, stability_seconds - elapsed))
            first_sample = False

        eligible = valid_login_passed and invalid_login_denied and health_passed and not self.simulated
        return {
            "authority": "independent_verifier",
            "simulated": self.simulated,
            "valid_login_passed": valid_login_passed,
            "invalid_login_denied": invalid_login_denied,
            "health_passed": health_passed,
            "stability_seconds": int(self.monotonic() - start),
            "observations": observations,
            "resolution_eligible": eligible,
        }
