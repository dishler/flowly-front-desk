from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DEBUG", "false")

from app.api.routes import telegram_webhook
from app.application.dto.normalized_message import NormalizedMessage
from app.infrastructure.telegram.parser import TelegramPayloadParser


class RecordingProcessor:
    def __init__(self) -> None:
        self.calls: list[NormalizedMessage] = []

    async def process(self, message: NormalizedMessage) -> dict:
        self.calls.append(message)
        return {"outbound_result": {"sent": True}, "reply_text": "ok"}


def _app_with_processor(processor: RecordingProcessor | None = None) -> FastAPI:
    app = FastAPI()
    app.state.message_processor = processor or RecordingProcessor()
    app.include_router(telegram_webhook.router, prefix="/webhooks")
    return app


def _valid_payload(**overrides) -> dict:
    payload = {
        "update_id": 123,
        "message": {
            "message_id": 456,
            "date": 1710000000,
            "chat": {"id": 789, "type": "private"},
            "from": {"id": 789, "is_bot": False, "first_name": "Test"},
            "text": "Привіт",
        },
    }
    payload.update(overrides)
    return payload


def test_valid_private_text_message_parses_to_normalized_message():
    message = TelegramPayloadParser.parse(_valid_payload())

    assert message is not None
    assert message.platform == "telegram"
    assert message.sender_id == "telegram:789"
    assert message.recipient_id == "telegram:789"
    assert message.message_mid == "telegram:update:123"
    assert message.user_message == "Привіт"
    assert message.audio_url is None
    assert message.timestamp is None


def test_parser_ignores_missing_update_id():
    assert TelegramPayloadParser.parse(_valid_payload(update_id=None)) is None


def test_parser_ignores_empty_text():
    assert TelegramPayloadParser.parse(
        _valid_payload(message={"chat": {"id": 789, "type": "private"}, "text": "   "})
    ) is None


def test_parser_handles_malformed_payload_safely():
    assert TelegramPayloadParser.parse({}) is None
    assert TelegramPayloadParser.parse({"update_id": 123, "message": "bad"}) is None


def test_parser_ignores_photo_voice_sticker_document_and_non_message_updates():
    unsupported_payloads = [
        _valid_payload(message={"chat": {"id": 789, "type": "private"}, "photo": [{"file_id": "p"}]}),
        _valid_payload(message={"chat": {"id": 789, "type": "private"}, "voice": {"file_id": "v"}}),
        _valid_payload(message={"chat": {"id": 789, "type": "private"}, "sticker": {"file_id": "s"}}),
        _valid_payload(message={"chat": {"id": 789, "type": "private"}, "document": {"file_id": "d"}}),
        {"update_id": 123, "callback_query": {"id": "cb"}},
        {"update_id": 123, "edited_message": {"chat": {"id": 789, "type": "private"}, "text": "edit"}},
    ]

    for payload in unsupported_payloads:
        assert TelegramPayloadParser.parse(payload) is None


def test_parser_ignores_group_and_supergroup_messages():
    group_payload = _valid_payload(
        message={"chat": {"id": -100, "type": "group"}, "text": "Привіт"}
    )
    supergroup_payload = _valid_payload(
        message={"chat": {"id": -100, "type": "supergroup"}, "text": "Привіт"}
    )

    assert TelegramPayloadParser.parse(group_payload) is None
    assert TelegramPayloadParser.parse(supergroup_payload) is None


def test_valid_webhook_secret_accepted_and_invokes_processor_once(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "secret-token")
    processor = RecordingProcessor()
    client = TestClient(_app_with_processor(processor))

    response = client.post(
        "/webhooks/telegram",
        json=_valid_payload(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["normalized_message"]["platform"] == "telegram"
    assert body["normalized_message"]["sender_id"] == "telegram:789"
    assert body["normalized_message"]["message_mid"] == "telegram:update:123"
    assert len(processor.calls) == 1
    assert processor.calls[0].user_message == "Привіт"


def test_invalid_webhook_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "secret-token")
    processor = RecordingProcessor()
    client = TestClient(_app_with_processor(processor))

    response = client.post(
        "/webhooks/telegram",
        json=_valid_payload(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid webhook secret"
    assert processor.calls == []


def test_missing_required_webhook_secret_is_rejected_in_production(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "")
    processor = RecordingProcessor()
    client = TestClient(_app_with_processor(processor))

    response = client.post("/webhooks/telegram", json=_valid_payload())

    assert response.status_code == 403
    assert processor.calls == []


def test_secret_not_leaked_in_logs_or_error_response(monkeypatch, caplog):
    secret = "super-secret-token"
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", secret)
    client = TestClient(_app_with_processor())
    caplog.set_level(logging.WARNING, logger="app.api.routes.telegram_webhook")

    response = client.post(
        "/webhooks/telegram",
        json=_valid_payload(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert response.status_code == 403
    assert secret not in logs
    assert secret not in response.text


def test_malformed_json_returns_400_after_valid_secret(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "secret-token")
    client = TestClient(_app_with_processor())

    response = client.post(
        "/webhooks/telegram",
        content="{",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON payload"


def test_unsupported_update_acknowledged_without_processing(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "production")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "secret-token")
    processor = RecordingProcessor()
    client = TestClient(_app_with_processor(processor))

    response = client.post(
        "/webhooks/telegram",
        json={"update_id": 123, "callback_query": {"id": "cb"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unsupported_update"}
    assert processor.calls == []


def test_dev_without_webhook_secret_allows_local_request(monkeypatch):
    monkeypatch.setattr(telegram_webhook.settings, "environment", "dev")
    monkeypatch.setattr(telegram_webhook.settings, "telegram_webhook_secret", "")
    processor = RecordingProcessor()
    client = TestClient(_app_with_processor(processor))

    response = client.post("/webhooks/telegram", json=_valid_payload())

    assert response.status_code == 200
    assert len(processor.calls) == 1
