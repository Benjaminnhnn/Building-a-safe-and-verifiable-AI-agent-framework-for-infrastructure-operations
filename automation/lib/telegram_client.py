"""Minimal operator-side Telegram client (standard library only).

Used by the approval bridge and the Independent Verifier on the operator's
machine. Credentials come from the environment or the Git-ignored release/.env
and are never printed. Every network failure is reported to the caller, so a
caller that needs a human decision can fail closed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / "release" / ".env"
KEYS = ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_USER_IDS")


class TelegramError(RuntimeError):
    pass


def load_config(env_file: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key in KEYS:
                    values[key] = value.strip().strip('"').strip("'")
    for key in KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


class TelegramClient:
    def __init__(self, config: dict[str, str] | None = None) -> None:
        config = config if config is not None else load_config()
        self._token = config.get("TELEGRAM_TOKEN", "")
        self.chat_id = config.get("TELEGRAM_CHAT_ID", "")
        self.admin_ids = {
            int(item) for item in config.get("TELEGRAM_ADMIN_USER_IDS", "").split(",") if item.strip().isdigit()
        }

    @property
    def configured(self) -> bool:
        return bool(self._token and self.chat_id)

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: float = 15) -> Any:
        if not self.configured:
            raise TelegramError("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is not configured")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                description = json.loads(exc.read()).get("description", "")
            except ValueError:
                description = ""
            raise TelegramError(f"{method} failed: HTTP {exc.code} {description}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise TelegramError(f"{method} failed: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body.get('description', 'unknown error')}")
        return body["result"]

    def send(self, text: str, buttons: list[list[dict[str, str]]] | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:4000]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return int(self.call("sendMessage", payload)["message_id"])

    def edit(self, message_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "message_id": message_id, "text": text[:4000]}
        payload["reply_markup"] = {"inline_keyboard": buttons or []}
        self.call("editMessageText", payload)

    def answer(self, callback_id: str, text: str) -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
        except TelegramError:
            pass  # the toast is cosmetic; the decision is already recorded

    def latest_update_offset(self) -> int:
        """Skip every update queued before now, so old button presses cannot count."""
        updates = self.call("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["callback_query"]})
        return (updates[-1]["update_id"] + 1) if updates else 0

    def callback_queries(self, offset: int, timeout: int = 20) -> tuple[list[dict[str, Any]], int]:
        updates = self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["callback_query"]},
            timeout=timeout + 10,
        )
        queries = [update["callback_query"] for update in updates if "callback_query" in update]
        next_offset = (updates[-1]["update_id"] + 1) if updates else offset
        return queries, next_offset


def notify(text: str) -> bool:
    """Best-effort message for progress lines; never raises."""
    try:
        client = TelegramClient()
        if not client.configured:
            return False
        client.send(text)
        return True
    except TelegramError:
        return False
