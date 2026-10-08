from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.probes import LoginObservation, MoodleAuthVerifier, MoodleLoginProbe


class _Session:
    def __init__(self, authenticated: bool) -> None:
        self.authenticated = authenticated
        self.cookies = {"MoodleSession": "opaque"} if authenticated else {}
        self.closed = False

    def get(self, *args, **kwargs):
        return SimpleNamespace(
            text='<input type="hidden" name="logintoken" value="csrf">',
            url=args[0],
            status_code=200,
            raise_for_status=lambda: None,
        )

    def post(self, *args, **kwargs):
        authenticated = self.authenticated
        return SimpleNamespace(
            url=args[0],
            status_code=303 if authenticated else 200,
            headers={"Location": "/my/"} if authenticated else {},
            raise_for_status=lambda: None,
        )

    def close(self):
        self.closed = True


def test_login_probe_requires_https_except_explicit_loopback() -> None:
    with pytest.raises(ValueError, match="requires HTTPS"):
        MoodleLoginProbe("http://moodle.example")
    assert MoodleLoginProbe("http://127.0.0.1:8080", allow_local_http=True)
    with pytest.raises(ValueError, match="cannot be disabled"):
        MoodleLoginProbe("https://moodle.example", verify_tls=False)


def test_login_probe_recognizes_success_and_denial_without_returning_identity() -> None:
    probe = MoodleLoginProbe("https://moodle.example", session_factory=lambda: _Session(True))
    assert probe.login("synthetic", "secret").authenticated is True
    denied = MoodleLoginProbe("https://moodle.example", session_factory=lambda: _Session(False))
    assert denied.login("synthetic", "secret").outcome == "denied"


def test_login_probe_does_not_follow_cross_origin_redirect_with_credentials() -> None:
    class RedirectSession(_Session):
        post_kwargs = None

        def post(self, *args, **kwargs):
            self.post_kwargs = kwargs
            return SimpleNamespace(
                url=args[0],
                status_code=302,
                headers={"Location": "https://attacker.example/collect"},
                raise_for_status=lambda: None,
            )

    session = RedirectSession(True)
    probe = MoodleLoginProbe(
        "https://moodle.example", session_factory=lambda: session
    )

    result = probe.login("synthetic", "secret")

    assert result.authenticated is False
    assert result.outcome == "error"
    assert session.post_kwargs["allow_redirects"] is False
    assert session.closed is True


def test_verifier_fails_closed_if_health_is_green_but_login_fails() -> None:
    class Clock:
        now = 0.0

        def advance(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()

    class Probe:
        simulated = False
        calls = 0

        def login(self, username: str, password: str) -> LoginObservation:
            self.calls += 1
            return LoginObservation(authenticated=username == "valid", elapsed_ms=1,
                                    outcome="authenticated" if username == "valid" else "denied")

    probe = Probe()
    result = MoodleAuthVerifier(
        probe, lambda: True, monotonic=lambda: clock.now, sleep=clock.advance
    ).verify(
        valid_username="invalid", valid_password="secret",
        denied_username="invalid-user", denied_password="secret",
        stability_seconds=120, sample_interval_seconds=60,
    )
    assert result["health_passed"] is True
    assert result["resolution_eligible"] is False
    assert result["stability_seconds"] == 120
    assert probe.calls == 4
    assert "secret" not in str(result)


@pytest.mark.parametrize("health_value", ["false", 1])
def test_verifier_rejects_truthy_non_boolean_health_values(health_value) -> None:
    class Clock:
        now = 0.0

        def advance(self, seconds: float) -> None:
            self.now += seconds

    class Probe:
        simulated = False

        def login(self, username: str, password: str) -> LoginObservation:
            authenticated = username == "valid"
            return LoginObservation(
                authenticated=authenticated,
                elapsed_ms=1,
                outcome="authenticated" if authenticated else "denied",
            )

    clock = Clock()

    def health():
        return health_value

    health.simulated = False
    result = MoodleAuthVerifier(
        Probe(), health, monotonic=lambda: clock.now, sleep=clock.advance
    ).verify(
        valid_username="valid",
        valid_password="secret",
        denied_username="invalid",
        denied_password="secret",
        stability_seconds=120,
        sample_interval_seconds=60,
    )

    assert result["health_passed"] is False
    assert result["resolution_eligible"] is False
    assert all(item["health"] is False for item in result["observations"])


def test_verifier_rejects_inconsistent_login_observation() -> None:
    class Clock:
        now = 0.0

        def advance(self, seconds: float) -> None:
            self.now += seconds

    class Probe:
        simulated = False

        def login(self, username: str, password: str) -> LoginObservation:
            return LoginObservation(
                authenticated=True, elapsed_ms=1, outcome="denied"
            )

    clock = Clock()

    def health() -> bool:
        return True

    health.simulated = False
    result = MoodleAuthVerifier(
        Probe(), health, monotonic=lambda: clock.now, sleep=clock.advance
    ).verify(
        valid_username="valid",
        valid_password="secret",
        denied_username="invalid",
        denied_password="secret",
        stability_seconds=120,
        sample_interval_seconds=60,
    )

    assert result["valid_login_passed"] is False
    assert result["invalid_login_denied"] is False
    assert result["resolution_eligible"] is False


def test_verifier_requires_120_seconds_and_validates_negative_probe() -> None:
    class Probe:
        def login(self, username: str, password: str) -> LoginObservation:
            return LoginObservation(authenticated=(username == "valid"), elapsed_ms=1,
                                    outcome="authenticated" if username == "valid" else "denied")

    verifier = MoodleAuthVerifier(Probe(), lambda: True)
    with pytest.raises(ValueError, match="120 seconds"):
        verifier.verify(valid_username="valid", valid_password="x", denied_username="invalid", denied_password="x", stability_seconds=119)


@pytest.mark.parametrize("value", [True, 120.5])
def test_verifier_requires_integer_stability_window(value) -> None:
    verifier = MoodleAuthVerifier(
        lambda: None, lambda: True
    )
    with pytest.raises(ValueError, match="at least 120 seconds"):
        verifier.verify(
            valid_username="valid",
            valid_password="x",
            denied_username="invalid",
            denied_password="x",
            stability_seconds=value,
        )


def test_verifier_requires_valid_login_and_denial_at_both_window_edges() -> None:
    class Clock:
        now = 0.0

        def advance(self, seconds: float) -> None:
            self.now += seconds

    class Probe:
        simulated = False
        calls = 0

        def login(self, username: str, password: str) -> LoginObservation:
            self.calls += 1
            return LoginObservation(authenticated=username == "valid", elapsed_ms=1,
                                    outcome="authenticated" if username == "valid" else "denied")

    clock = Clock()
    probe = Probe()
    def health() -> bool:
        return True

    health.simulated = False
    result = MoodleAuthVerifier(probe, health, monotonic=lambda: clock.now, sleep=clock.advance).verify(
        valid_username="valid", valid_password="x", denied_username="invalid", denied_password="x",
        stability_seconds=120, sample_interval_seconds=60,
    )
    assert result["resolution_eligible"] is True
    assert result["simulated"] is False
    assert probe.calls == 4
    assert len(result["observations"]) == 3
    assert result["observations"][1]["valid_login"] is None

