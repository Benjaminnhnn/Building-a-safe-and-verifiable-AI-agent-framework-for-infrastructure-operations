"""Read Okta System Log failures for the reviewed Moodle app and notify operators.

This is a read-only, human-only lane. It never submits an executor request or
marks an incident resolved. The cursor and delivered event IDs live in the
agent evidence volume so a restart does not replay old notifications.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.moodle.safe_action_notify import notify

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "app.oauth2.authorize",
    "user.authentication.sso",
    "app.generic.unauth_app_access_attempt",
    "application.policy.sign_on.deny_access",
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _clean(value: object, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[:limit] or "không có trong Okta System Log"


def failure_for_moodle(event: dict, app_id: str) -> bool:
    """Accept failed Moodle app events, including authorize errors before login."""
    kind = event.get("eventType")
    if kind not in EVENT_TYPES:
        return False
    if kind == "app.oauth2.authorize":
        if str((event.get("outcome") or {}).get("result") or "").upper() != "FAILURE":
            return False
        debug = (event.get("debugContext") or {}).get("debugData") or {}
        # Failed authorize requests may have no AppInstance target. The URL's
        # client_id is the only reliable app discriminator in this event.
        client_ids = parse_qs(urlparse(str(debug.get("url") or "")).query).get("client_id", [])
        return client_ids == [app_id]
    targets = event.get("target") or []
    if not any(
        isinstance(target, dict)
        and target.get("type") == "AppInstance"
        and target.get("id") == app_id
        for target in targets
    ):
        return False
    if kind in {"app.generic.unauth_app_access_attempt", "application.policy.sign_on.deny_access"}:
        return True
    return str((event.get("outcome") or {}).get("result") or "").upper() in {"FAILURE", "DENY"}


def format_failure(event: dict) -> str:
    actor = event.get("actor") or {}
    outcome = event.get("outcome") or {}
    kind = str(event.get("eventType") or "")
    if kind == "app.oauth2.authorize":
        user = "Chưa xác định (Okta từ chối trước khi nhập tài khoản)"
        login = user
        if outcome.get("reason") == "illegal_redirect_uri_enhanced":
            redirect = _clean(((event.get("debugContext") or {}).get("debugData") or {}).get("redirectUri"), 300)
            guidance = (
                "Trong Okta app Moodle, thêm đúng Sign-in redirect URI: " + redirect
                + ". Sau khi lưu, bắt đầu đăng nhập lại từ trang Moodle."
            )
        else:
            guidance = "Kiểm tra yêu cầu authorize của app Moodle trong Okta System Log và cấu hình OIDC."
    elif kind == "app.generic.unauth_app_access_attempt":
        user = _clean(actor.get("displayName"))
        login = _clean(actor.get("alternateId"))
        guidance = (
            "Kiểm tra user có thuộc group được gán app Moodle trên Okta không. "
            "Nếu group đến từ Entra, sửa membership tại Entra rồi chạy Full import ở Okta."
        )
    elif kind == "application.policy.sign_on.deny_access":
        user = _clean(actor.get("displayName"))
        login = _clean(actor.get("alternateId"))
        guidance = "Kiểm tra authentication policy, group và MFA của app Moodle trong Okta."
    else:
        user = _clean(actor.get("displayName"))
        login = _clean(actor.get("alternateId"))
        guidance = (
            "Mở Okta System Log theo Event ID để xác định bước bị từ chối; "
            "kiểm tra assignment, policy và MFA trước khi sửa."
        )
    return (
        "🚨 ĐĂNG NHẬP SSO MOODLE THẤT BẠI\n"
        f"Người dùng: {user}\n"
        f"Login: {login}\n"
        f"Lỗi Okta: {_clean(outcome.get('reason') or event.get('displayMessage'))}\n"
        f"Loại sự kiện: {_clean(kind)}\n"
        f"Event ID: {_clean(event.get('uuid'), 80)}\n"
        f"Hướng xử lý: {guidance}\n"
        "Quyết định: chỉ người vận hành xử lý; Agent không tự cấp quyền hoặc thay đổi SSO."
    )


class OktaLogs:
    def __init__(self, org: str, client_id: str, kid: str, key_file: Path) -> None:
        parsed = urlparse(org)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("OKTA_ORG_URL must be an HTTPS org origin")
        self.org = org.rstrip("/")
        self.client_id = client_id
        self.kid = kid
        self.session = requests.Session()
        self.key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        if not isinstance(self.key, rsa.RSAPrivateKey):
            raise ValueError("Okta service app requires the reviewed RSA private key")
        self.token = ""
        self.token_expires_at = 0.0

    def _assertion(self) -> str:
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "kid": self.kid}
        payload = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": f"{self.org}/oauth2/v1/token",
            "iat": now,
            "exp": now + 300,
            "jti": str(uuid.uuid4()),
        }
        signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
        signature = self.key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64url(signature)}"

    def _refresh(self) -> None:
        response = self.session.post(
            f"{self.org}/oauth2/v1/token",
            data={
                "grant_type": "client_credentials",
                "scope": "okta.logs.read",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": self._assertion(),
            },
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if "okta.logs.read" not in str(body.get("scope") or "").split():
            raise RuntimeError("Okta token lacks okta.logs.read")
        self.token = body["access_token"]
        self.token_expires_at = time.time() + max(int(body.get("expires_in", 3600)) - 60, 30)

    def page(self, url: str) -> tuple[list[dict], str | None]:
        if urlparse(url).netloc != urlparse(self.org).netloc or not url.startswith(self.org + "/api/v1/logs?"):
            raise ValueError("Okta pagination URL is outside the System Log endpoint")
        if time.time() >= self.token_expires_at:
            self._refresh()
        response = self.session.get(
            url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}, timeout=20
        )
        if response.status_code == 401:
            self._refresh()
            response = self.session.get(
                url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}, timeout=20
            )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise ValueError("Okta System Log response must be a list")
        return body, (response.links.get("next") or {}).get("url")


def _initial_url(org: str) -> str:
    # Start at process startup. Existing failures are historical, not new alerts.
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    condition = " or ".join(f'eventType eq "{kind}"' for kind in EVENT_TYPES)
    return org + "/api/v1/logs?" + urlencode(
        {"since": since, "sortOrder": "ASCENDING", "limit": "100", "filter": condition}
    )


def run() -> None:
    org = os.environ["OKTA_ORG_URL"].rstrip("/")
    app_id = os.environ["OKTA_MOODLE_APP_ID"]
    if not app_id.startswith("0oa"):
        raise ValueError("OKTA_MOODLE_APP_ID must identify the reviewed Okta app")
    client = OktaLogs(
        org,
        os.environ["OKTA_API_CLIENT_ID"],
        os.environ["OKTA_API_PRIVATE_KEY_ID"],
        Path(os.environ["OKTA_API_PRIVATE_KEY_FILE"]),
    )
    db_path = Path(os.getenv("OKTA_SSO_MONITOR_DB", "/app/data/okta-sso-monitor.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS delivered (event_id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY CHECK(id=1), url TEXT NOT NULL)")
        db.commit()
        row = db.execute("SELECT url FROM cursor WHERE id=1").fetchone()
        url = row[0] if row else _initial_url(org)
        while True:
            try:
                # Bound each pass so a busy org cannot starve the polling interval.
                for _ in range(10):
                    events, next_url = client.page(url)
                    for event in events:
                        if not isinstance(event, dict) or not failure_for_moodle(event, app_id):
                            continue
                        event_id = str(event.get("uuid") or "")
                        if not event_id or db.execute("SELECT 1 FROM delivered WHERE event_id=?", (event_id,)).fetchone():
                            continue
                        if not notify(format_failure(event)):
                            raise RuntimeError("Telegram delivery failed; Okta cursor was not advanced")
                        db.execute(
                            "INSERT INTO delivered(event_id,sent_at) VALUES(?,?)",
                            (event_id, datetime.now(timezone.utc).isoformat()),
                        )
                        db.commit()
                    if not next_url:
                        raise RuntimeError("Okta polling response lacked next cursor")
                    url = next_url
                    db.execute("INSERT OR REPLACE INTO cursor(id,url) VALUES(1,?)", (url,))
                    db.commit()
                    if not events:
                        break
            except Exception as exc:
                logger.warning("Okta SSO monitor pass failed: error_type=%s", type(exc).__name__)
            time.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
