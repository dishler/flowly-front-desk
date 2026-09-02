from __future__ import annotations

from app.application.services.outbound_service import OutboundService


class RecordingMetaClient:
    def __init__(self, result: dict | None = None) -> None:
        self.calls = []
        self.result = result or {"sent": True, "platform": "instagram"}

    def send_text_message(self, platform: str, recipient_id: str, text: str) -> dict:
        self.calls.append(
            {
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        return self.result


class RecordingTelegramClient:
    def __init__(self, result: dict | None = None) -> None:
        self.calls = []
        self.result = result or {"sent": True, "platform": "telegram"}

    def send_text_message(self, recipient_id: str, text: str) -> dict:
        self.calls.append(
            {
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        return self.result


def test_instagram_routes_to_meta_client_only():
    meta_client = RecordingMetaClient(result={"sent": True, "meta_response": {"ok": True}})
    telegram_client = RecordingTelegramClient()
    service = OutboundService(meta_client=meta_client, telegram_client=telegram_client)

    result = service.send_reply(
        platform="instagram",
        recipient_id="ig-user-1",
        text="hello instagram",
    )

    assert result == {"sent": True, "meta_response": {"ok": True}}
    assert meta_client.calls == [
        {
            "platform": "instagram",
            "recipient_id": "ig-user-1",
            "text": "hello instagram",
        }
    ]
    assert telegram_client.calls == []


def test_telegram_routes_to_telegram_client_only():
    meta_client = RecordingMetaClient()
    telegram_client = RecordingTelegramClient(result={"sent": True, "telegram_response": {"ok": True}})
    service = OutboundService(meta_client=meta_client, telegram_client=telegram_client)

    result = service.send_reply(
        platform="telegram",
        recipient_id="telegram:12345",
        text="hello telegram",
    )

    assert result == {"sent": True, "telegram_response": {"ok": True}}
    assert telegram_client.calls == [
        {
            "recipient_id": "telegram:12345",
            "text": "hello telegram",
        }
    ]
    assert meta_client.calls == []


def test_telegram_failure_returns_failure_without_meta_fallback():
    meta_client = RecordingMetaClient()
    telegram_client = RecordingTelegramClient(
        result={
            "sent": False,
            "stub": False,
            "platform": "telegram",
            "recipient_id": "telegram:12345",
            "text": "hello telegram",
            "error_type": "TimeoutException",
        }
    )
    service = OutboundService(meta_client=meta_client, telegram_client=telegram_client)

    result = service.send_reply(
        platform="telegram",
        recipient_id="telegram:12345",
        text="hello telegram",
    )

    assert result["sent"] is False
    assert result["error_type"] == "TimeoutException"
    assert len(telegram_client.calls) == 1
    assert meta_client.calls == []


def test_unsupported_platform_fails_safely():
    meta_client = RecordingMetaClient()
    telegram_client = RecordingTelegramClient()
    service = OutboundService(meta_client=meta_client, telegram_client=telegram_client)

    result = service.send_reply(
        platform="sms",
        recipient_id="user-1",
        text="hello",
    )

    assert result == {
        "sent": False,
        "stub": True,
        "reason": "Unsupported platform=sms",
        "platform": "sms",
        "recipient_id": "user-1",
        "text": "hello",
    }
    assert meta_client.calls == []
    assert telegram_client.calls == []
