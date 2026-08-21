from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DEBUG", "false")

from app.api.routes import meta_webhook
from app.application.dto.normalized_message import NormalizedMessage
from app.application.services.booking_service import BookingService
from app.application.services.calendar_service import CalendarService
from app.application.services.dedup_service import DedupService
from app.application.services.intent_service import IntentService
from app.application.services.language_service import LanguageService
from app.application.services.memory_service import MemoryService
from app.application.services.message_processor import MessageProcessor
from app.application.services.redis_dedup_service import RedisDedupService
from app.application.services.reply_service import ReplyService


class DummyOutboundService:
    def __init__(self, *, sent: bool = True, raise_on_send: bool = False) -> None:
        self.sent = sent
        self.raise_on_send = raise_on_send
        self.calls = []

    def send_reply(self, platform: str, recipient_id: str, text: str) -> dict:
        self.calls.append(
            {
                "platform": platform,
                "recipient_id": recipient_id,
                "text": text,
            }
        )
        if self.raise_on_send:
            raise RuntimeError("send failed")
        return {"sent": self.sent}


class DummySpeechService:
    def __init__(self, *, raise_on_transcribe: bool = False) -> None:
        self.raise_on_transcribe = raise_on_transcribe

    async def transcribe_audio(self, file_url: str) -> str:
        if self.raise_on_transcribe:
            raise RuntimeError("transcription failed")
        return ""


class RecordingAIService:
    def try_generate_reply(
        self,
        user_message: str,
        history=None,
        grounding_context=None,
        system_instruction=None,
    ) -> dict:
        return {"reply_text": None}


class FakeRedis:
    def __init__(self) -> None:
        self.store = {}
        self.set_calls = []

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        self.set_calls.append(
            {
                "key": key,
                "value": value,
                "ex": ex,
                "nx": nx,
            }
        )
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key: str):
        return self.store.get(key)

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class FailingProcessor:
    async def process(self, message: NormalizedMessage) -> dict:
        raise RuntimeError("processor failed")


class RecordingProcessor:
    def __init__(self) -> None:
        self.calls = []

    async def process(self, message: NormalizedMessage) -> dict:
        self.calls.append(message)
        return {"sent": True}


def _message(message_mid: str = "mid-1", text: str = "привіт", audio_url: str | None = None):
    return NormalizedMessage(
        platform="instagram",
        sender_id="user-1",
        recipient_id="bot-1",
        message_mid=message_mid,
        user_message=text,
        audio_url=audio_url,
    )


def _processor(
    *,
    dedup_service=None,
    outbound_service=None,
    speech_service=None,
):
    memory_service = MemoryService()
    return MessageProcessor(
        memory_service=memory_service,
        reply_service=ReplyService(
            ai_service=RecordingAIService(),
            memory_service=memory_service,
            knowledge_service=None,
        ),
        outbound_service=outbound_service or DummyOutboundService(),
        dedup_service=dedup_service or DedupService(),
        intent_service=IntentService(),
        booking_service=BookingService(
            calendar_service=CalendarService(),
            language_service=LanguageService(),
        ),
        speech_service=speech_service or DummySpeechService(),
    )


def test_first_non_empty_message_mid_can_be_claimed():
    dedup = DedupService()

    assert dedup.claim_processing("mid-1")


def test_second_claim_for_same_mid_is_rejected():
    dedup = DedupService()

    assert dedup.claim_processing("mid-1")
    assert not dedup.claim_processing("mid-1")


@pytest.mark.asyncio
async def test_successful_outbound_finalizes_message_as_processed():
    dedup = DedupService()
    processor = _processor(dedup_service=dedup)

    result = await processor.process(_message("mid-1"))

    assert result["outbound_result"] == {"sent": True}
    assert dedup.is_duplicate("mid-1")


@pytest.mark.asyncio
async def test_later_retry_after_successful_outbound_is_skipped_as_duplicate():
    outbound = DummyOutboundService()
    processor = _processor(outbound_service=outbound)

    await processor.process(_message("mid-1"))
    retry_result = await processor.process(_message("mid-1"))

    assert retry_result["intent"] == "duplicate_skipped"
    assert len(outbound.calls) == 1


@pytest.mark.asyncio
async def test_outbound_sent_false_does_not_permanently_mark_processed():
    dedup = DedupService()
    processor = _processor(
        dedup_service=dedup,
        outbound_service=DummyOutboundService(sent=False),
    )

    with pytest.raises(RuntimeError, match="Outbound reply failed"):
        await processor.process(_message("mid-1"))

    assert not dedup.is_duplicate("mid-1")


@pytest.mark.asyncio
async def test_retry_after_outbound_sent_false_can_process_again():
    dedup = DedupService()
    failed_outbound = DummyOutboundService(sent=False)
    processor = _processor(dedup_service=dedup, outbound_service=failed_outbound)

    with pytest.raises(RuntimeError, match="Outbound reply failed"):
        await processor.process(_message("mid-1"))

    successful_outbound = DummyOutboundService(sent=True)
    retry_processor = _processor(dedup_service=dedup, outbound_service=successful_outbound)
    result = await retry_processor.process(_message("mid-1"))

    assert result["outbound_result"] == {"sent": True}
    assert len(successful_outbound.calls) == 1


@pytest.mark.asyncio
async def test_outbound_exception_does_not_permanently_mark_processed():
    dedup = DedupService()
    processor = _processor(
        dedup_service=dedup,
        outbound_service=DummyOutboundService(raise_on_send=True),
    )

    with pytest.raises(RuntimeError, match="send failed"):
        await processor.process(_message("mid-1"))

    assert not dedup.is_duplicate("mid-1")


@pytest.mark.asyncio
async def test_processing_exception_does_not_permanently_mark_processed():
    dedup = DedupService()
    processor = _processor(
        dedup_service=dedup,
        speech_service=DummySpeechService(raise_on_transcribe=True),
    )

    with pytest.raises(RuntimeError, match="transcription failed"):
        await processor.process(_message("mid-1", text="", audio_url="https://media.example/audio.ogg"))

    assert not dedup.is_duplicate("mid-1")


def test_redis_claim_uses_set_nx_semantics():
    fake_redis = FakeRedis()
    dedup = RedisDedupService(redis_client=fake_redis)

    assert dedup.claim_processing("mid-1")

    assert fake_redis.set_calls[-1]["nx"] is True
    assert fake_redis.set_calls[-1]["value"] == RedisDedupService.PROCESSING_VALUE
    assert fake_redis.set_calls[-1]["ex"] == RedisDedupService.PROCESSING_TTL_SECONDS


def test_completed_dedup_ttl_uses_message_ttl_not_hardcoded_600():
    fake_redis = FakeRedis()
    dedup = RedisDedupService(redis_client=fake_redis)

    dedup.mark_processed("mid-1")

    assert fake_redis.set_calls[-1]["value"] == RedisDedupService.COMPLETED_VALUE
    assert fake_redis.set_calls[-1]["ex"] == dedup.settings.redis_message_ttl_seconds
    assert fake_redis.set_calls[-1]["ex"] != 600


@pytest.mark.asyncio
async def test_empty_message_mid_preserves_existing_non_deduped_behavior():
    outbound = DummyOutboundService()
    processor = _processor(outbound_service=outbound)

    await processor.process(_message("", text="привіт"))
    await processor.process(_message("", text="привіт"))

    assert len(outbound.calls) == 2


def test_webhook_processing_exception_returns_500_for_provider_retry(monkeypatch):
    monkeypatch.setattr(meta_webhook.settings, "environment", "dev")
    monkeypatch.setattr(meta_webhook.settings, "meta_app_secret", "")

    app = FastAPI()
    app.state.message_processor = FailingProcessor()
    app.include_router(meta_webhook.router, prefix="/webhooks")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/webhooks/meta",
        json={
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "user-1"},
                            "recipient": {"id": "bot-1"},
                            "message": {
                                "mid": "mid-1",
                                "text": "привіт",
                            },
                        }
                    ]
                }
            ]
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "message_processing_failed"


def test_instagram_inbound_message_processes_with_sender_as_customer(monkeypatch):
    monkeypatch.setattr(meta_webhook.settings, "environment", "dev")
    monkeypatch.setattr(meta_webhook.settings, "meta_app_secret", "")

    processor = RecordingProcessor()
    app = FastAPI()
    app.state.message_processor = processor
    app.include_router(meta_webhook.router, prefix="/webhooks")
    client = TestClient(app)

    response = client.post(
        "/webhooks/meta",
        json={
            "object": "instagram",
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "customer-ig-user"},
                            "recipient": {"id": "business-ig-account"},
                            "message": {
                                "mid": "mid-ig-1",
                                "text": "Привіт",
                            },
                        }
                    ]
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["normalized_message"]["platform"] == "instagram"
    assert body["normalized_message"]["sender_id"] == "customer-ig-user"
    assert len(processor.calls) == 1
    assert processor.calls[0].sender_id == "customer-ig-user"


def test_instagram_echo_message_is_ignored_before_processing(monkeypatch):
    monkeypatch.setattr(meta_webhook.settings, "environment", "dev")
    monkeypatch.setattr(meta_webhook.settings, "meta_app_secret", "")

    processor = RecordingProcessor()
    app = FastAPI()
    app.state.message_processor = processor
    app.include_router(meta_webhook.router, prefix="/webhooks")
    client = TestClient(app)

    response = client.post(
        "/webhooks/meta",
        json={
            "object": "instagram",
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "business-ig-account"},
                            "recipient": {"id": "customer-ig-user"},
                            "message": {
                                "mid": "mid-ig-echo-1",
                                "text": "Вітаю! Чим можемо допомогти?",
                                "is_echo": True,
                            },
                        }
                    ]
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "echo_or_self_message",
    }
    assert processor.calls == []
