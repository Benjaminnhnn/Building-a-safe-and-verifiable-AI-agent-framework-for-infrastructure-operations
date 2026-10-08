from unittest.mock import Mock, patch

from utils import telegram_bot


def test_webhook_setup_sends_secret_and_restricts_updates() -> None:
    response = Mock()
    response.json.return_value = {"ok": True}
    with (
        patch.object(telegram_bot, "TELEGRAM_TOKEN", "bot-token"),
        patch.object(telegram_bot.requests, "post", return_value=response) as post,
    ):
        assert telegram_bot.set_telegram_webhook("https://agent.example", "random-secret_123")

    assert post.call_args.kwargs["json"] == {
        "url": "https://agent.example/telegram/webhook",
        "secret_token": "random-secret_123",
        "allowed_updates": ["message", "edited_message", "callback_query"],
    }


def test_webhook_setup_fails_closed_without_valid_secret_or_https() -> None:
    with (
        patch.object(telegram_bot, "TELEGRAM_TOKEN", "bot-token"),
        patch.object(telegram_bot.requests, "post") as post,
    ):
        assert not telegram_bot.set_telegram_webhook("https://agent.example", "not valid")
        assert not telegram_bot.set_telegram_webhook("http://agent.example", "valid_secret")

    post.assert_not_called()
