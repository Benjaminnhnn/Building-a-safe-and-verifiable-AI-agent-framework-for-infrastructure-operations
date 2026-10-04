from __future__ import annotations

from pathlib import Path

import pytest

from core.live_adapters import MoodleAuthVerificationAdapter, _read_secret


def test_read_secret_rejects_empty_file(tmp_path: Path) -> None:
    secret = tmp_path / "credential"
    secret.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        _read_secret(secret)


def test_adapter_emits_only_redacted_boolean_probe_results(tmp_path: Path, monkeypatch) -> None:
    username = tmp_path / "username"
    password = tmp_path / "password"
    username.write_text("synthetic-private-account", encoding="utf-8")
    password.write_text("synthetic-private-password", encoding="utf-8")
    adapter = MoodleAuthVerificationAdapter(
        "https://moodle.example", username, password,
    )

    class FakeVerifier:
        def __init__(self, login, health):
            pass

        def verify(self, **kwargs):
            assert kwargs["valid_username"] == "synthetic-private-account"
            assert kwargs["valid_password"] == "synthetic-private-password"
            return {
                "authority": "independent_verifier",
                "valid_login_passed": True,
                "invalid_login_denied": True,
                "health_passed": True,
                "resolution_eligible": True,
                "observations": [{"valid_login": True}],
            }

    monkeypatch.setattr("core.live_adapters.MoodleAuthVerifier", FakeVerifier)
    result = adapter.verify(stability_seconds=120)
    serialized = str(result)
    assert "synthetic-private-account" not in serialized
    assert "synthetic-private-password" not in serialized
    assert result["resolution_eligible"] is True
