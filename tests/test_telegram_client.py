from __future__ import annotations

import httpx

from app.core.config import Settings
from app.infrastructure.telegram.client import TelegramClient


def _settings(**overrides) -> Settings:
    values = {
        "telegram_send_enabled": True,
        "telegram_bot_token": "test-bot-token",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_disabled_telegram_sending_returns_stub_without_http_call(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("HTTP client should not be created")

    monkeypatch.setattr("app.infrastructure.telegram.client.httpx.Client", FailingClient)

    client = TelegramClient(settings=_settings(telegram_send_enabled=False))

    result = client.send_text_message("telegram:12345", "hello")

    assert result == {
        "sent": False,
        "stub": True,
        "reason": "TELEGRAM_SEND_ENABLED=false",
        "platform": "telegram",
        "recipient_id": "telegram:12345",
        "text": "hello",
    }


def test_extract_chat_id_from_valid_telegram_recipient():
    assert TelegramClient.extract_chat_id("telegram:12345") == "12345"
    assert TelegramClient.extract_chat_id(" telegram:12345 ") == "12345"


def test_malformed_telegram_recipient_is_rejected_without_http_call(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("HTTP client should not be created")

    monkeypatch.setattr("app.infrastructure.telegram.client.httpx.Client", FailingClient)

    client = TelegramClient(settings=_settings())

    result = client.send_text_message("instagram:12345", "hello")

    assert result == {
        "sent": False,
        "stub": True,
        "reason": "Malformed Telegram recipient_id",
        "platform": "telegram",
        "recipient_id": "instagram:12345",
        "text": "hello",
    }
    assert TelegramClient.extract_chat_id("telegram:") == ""
    assert TelegramClient.extract_chat_id("telegram:-12345") == ""
    assert TelegramClient.extract_chat_id("telegram:abc") == ""


def test_successful_mocked_send_message(monkeypatch):
    calls = []

    class SuccessfulClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, json: dict):
            calls.append({"url": url, "json": json, "timeout": self.timeout})
            request = httpx.Request("POST", url)
            return httpx.Response(200, request=request, json={"ok": True, "result": {"message_id": 7}})

    monkeypatch.setattr("app.infrastructure.telegram.client.httpx.Client", SuccessfulClient)

    result = TelegramClient(settings=_settings()).send_text_message("telegram:12345", "hello")

    assert calls == [
        {
            "url": "https://api.telegram.org/bottest-bot-token/sendMessage",
            "json": {"chat_id": "12345", "text": "hello"},
            "timeout": 20.0,
        }
    ]
    assert result == {
        "sent": True,
        "stub": False,
        "platform": "telegram",
        "recipient_id": "telegram:12345",
        "text": "hello",
        "telegram_response": {"ok": True, "result": {"message_id": 7}},
    }


def test_telegram_api_failure_returns_sanitized_failure(monkeypatch):
    token = "secret-token"

    class FailingApiClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, json: dict):
            request = httpx.Request("POST", url)
            return httpx.Response(
                400,
                request=request,
                json={"ok": False, "description": f"Bad Request: chat not found {token}"},
            )

    monkeypatch.setattr("app.infrastructure.telegram.client.httpx.Client", FailingApiClient)

    result = TelegramClient(
        settings=_settings(telegram_bot_token=token)
    ).send_text_message("telegram:12345", "hello")

    assert result["sent"] is False
    assert result["stub"] is False
    assert result["status_code"] == 400
    assert result["error_type"] == "HTTPStatusError"
    assert "chat not found" in result["telegram_error_body"]
    assert "[REDACTED_TELEGRAM_BOT_TOKEN]" in result["telegram_error_body"]
    assert token not in repr(result)


def test_telegram_network_failure_returns_sanitized_failure(monkeypatch):
    token = "secret-token"

    class TimeoutClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url: str, json: dict):
            request = httpx.Request("POST", url)
            raise httpx.TimeoutException(
                f"timed out url={url}",
                request=request,
            )

    monkeypatch.setattr("app.infrastructure.telegram.client.httpx.Client", TimeoutClient)

    result = TelegramClient(
        settings=_settings(telegram_bot_token=token)
    ).send_text_message("telegram:12345", "hello")

    assert result == {
        "sent": False,
        "stub": False,
        "platform": "telegram",
        "recipient_id": "telegram:12345",
        "text": "hello",
        "error_type": "TimeoutException",
    }
    assert token not in repr(result)


def test_missing_telegram_bot_token_returns_stub_without_http_call(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("HTTP client should not be created")

    monkeypatch.setattr("app.infrastructure.telegram.client.httpx.Client", FailingClient)

    client = TelegramClient(settings=_settings(telegram_bot_token=""))

    result = client.send_text_message("telegram:12345", "hello")

    assert result == {
        "sent": False,
        "stub": True,
        "reason": "Missing TELEGRAM_BOT_TOKEN",
        "platform": "telegram",
        "recipient_id": "telegram:12345",
        "text": "hello",
    }
