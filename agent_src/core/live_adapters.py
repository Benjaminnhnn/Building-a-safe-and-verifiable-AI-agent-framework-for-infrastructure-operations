"""Read-only adapters used by an independently operated verifier process."""

from __future__ import annotations

from pathlib import Path
import requests

from core.probes import MoodleAuthVerifier, MoodleLoginProbe


def _read_secret(path: str | Path) -> str:
    value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise ValueError("credential file is empty")
    return value


class MoodleAuthVerificationAdapter:
    """Run read-only login checks; returns no account names or credentials."""

    def __init__(
        self,
        base_url: str,
        valid_username_file: str | Path,
        valid_password_file: str | Path,
        *,
        ca_bundle: str | Path | None = None,
        allow_local_http: bool = False,
    ) -> None:
        self.base_url = base_url
        self.valid_username_file = Path(valid_username_file)
        self.valid_password_file = Path(valid_password_file)
        self.ca_bundle = str(ca_bundle) if ca_bundle else True
        self.allow_local_http = allow_local_http

    def verify(
        self,
        *,
        stability_seconds: int = 120,
        sample_interval_seconds: int = 15,
        on_observation=None,
    ) -> dict:
        login = MoodleLoginProbe(
            self.base_url,
            verify_tls=self.ca_bundle,
            allow_local_http=self.allow_local_http,
        )

        def health() -> bool:
            try:
                response = requests.get(
                    self.base_url.rstrip("/") + "/healthz.php",
                    timeout=10,
                    verify=self.ca_bundle,
                )
                return response.status_code == 200
            except Exception:
                return False

        verifier = MoodleAuthVerifier(login, health)
        # A fixed synthetic non-existent username ensures the forbidden probe
        # checks denial. Its password is deliberately empty and never logged.
        result = verifier.verify(
            valid_username=_read_secret(self.valid_username_file),
            valid_password=_read_secret(self.valid_password_file),
            denied_username="aiops-invalid-probe-user",
            denied_password="invalid-probe-no-secret",
            stability_seconds=stability_seconds,
            sample_interval_seconds=sample_interval_seconds,
            on_observation=on_observation,
        )
        # Never return account identifiers or raw probe output to the agent.
        return result
